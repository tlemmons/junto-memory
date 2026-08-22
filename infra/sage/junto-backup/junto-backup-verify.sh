#!/usr/bin/env bash
#
# Backup verification watchdog for sage's junto (shared-memory MCP) backups.
# Runs via junto-backup-verify.timer, daily at 05:00 LOCAL (after the backup
# crons: chroma 03:00, mongo 03:15, sync-to-storm 04:00).
#
# Belt-and-suspenders alerting for a gap that already bit once: a chroma
# backup failed SILENTLY on 2026-08-04 (tar race on live SQLite) and nothing
# alerted. memory@junto owns the backup jobs + the tar-race root-cause fix;
# THIS is the durable host-side monitor. Ref: backlog_d59e8fc1b760.
#
# Catches:
#   - a daily local backup missing (cron didn't run / host rebooted through it)
#   - a local backup produced but truncated (< floor, or < 50% trailing median)
#   - offsite (storm) sync stale (>26h) or storm unreachable
#   - a failed run signature in the backup log (bonus; /tmp clears on reboot)
#   - the dir being empty (vacuous-pass guard: 0 files => ALERT, never a pass)
#
# On ANY failure: non-zero exit triggers systemd OnFailure=
# junto-backup-verify-alert.service, which emails Tom via msmtp.
#
# Every run states its denominator ("checked N of N") so a clean result is
# distinguishable from a result that examined nothing (evidence_invariant).
#
# Installed by: deploy.sh in this directory. Runs as user tlemmons on sage.
# Source-controlled at infra/sage/junto-backup/.

set -uo pipefail   # NOT -e: we want to run ALL checks and aggregate failures,
                   # not exit on the first one, so one alert names every fault.

# ---- Config ----
CHROMA_DIR="${CHROMA_DIR:-$HOME/chroma-backups}"
MONGO_DIR="${MONGO_DIR:-$HOME/mongo-backups}"
CHROMA_GLOB='chroma-backup-*.tar.gz'
MONGO_GLOB='mongo-backup-*.archive.gz'

# Absolute sanity floors. Observed 2026-08: chroma ~200 MB, mongo ~72 MB.
# Floors are deliberately well below observed to catch truncation, not drift.
CHROMA_MIN_BYTES=$(( 50 * 1024 * 1024 ))   # 50 MB
MONGO_MIN_BYTES=$(( 20 * 1024 * 1024 ))    # 20 MB

# A fresh backup must be newer than this (embedded filename timestamp age).
# Backups are daily; 26h = one day + 2h grace past the expected run.
MAX_AGE_SECONDS=$(( 26 * 3600 ))

# Trailing window for the median-size undersize check.
MEDIAN_WINDOW=7
UNDERSIZE_FRACTION_PCT=50   # newest must be >= 50% of trailing median

# Storm (offsite Windows host) — freshness of the synced copy.
STORM_KEY="${STORM_KEY:-$HOME/.ssh/storm-backup}"
STORM_USER="${STORM_USER:-Administrator}"
STORM_HOST="${STORM_HOST:-192.168.15.250}"
STORM_DIR_WIN='C:\SageBackup'
STORM_SSH_OPTS="-i $STORM_KEY -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new"

# Backup logs (supplementary signature check). /tmp clears on reboot — bonus only.
CHROMA_LOG=/tmp/chroma-backup.log
MONGO_LOG=/tmp/mongo-backup.log

LOG_TAG=junto-backup-verify

log() {
    logger -t "${LOG_TAG}" -p daemon.info -- "$*" 2>/dev/null || true
    echo "[$(date -u +%FT%TZ)] $*"
}
err() {
    logger -t "${LOG_TAG}" -p daemon.err -- "$*" 2>/dev/null || true
    echo "[$(date -u +%FT%TZ)] ERROR: $*" >&2
}

FAILURES=0
fail() { err "$*"; FAILURES=$(( FAILURES + 1 )); }

# Parse the embedded "YYYYMMDD-HHMMSS" stamp out of a backup filename and
# return its age in seconds (echoes age, or empty on parse failure).
embedded_age_seconds() {
    local fname="$1" stamp yyyymmdd hhmmss epoch
    stamp=$(sed -nE 's/.*-([0-9]{8})-([0-9]{6})\..*/\1-\2/p' <<< "$fname")
    [ -n "$stamp" ] || { echo ""; return; }
    yyyymmdd="${stamp%%-*}"
    hhmmss="${stamp##*-}"
    # Filenames are stamped in LOCAL time by the backup crons.
    epoch=$(date -d "${yyyymmdd:0:4}-${yyyymmdd:4:2}-${yyyymmdd:6:2} ${hhmmss:0:2}:${hhmmss:2:2}:${hhmmss:4:2}" +%s 2>/dev/null) || { echo ""; return; }
    echo $(( $(date +%s) - epoch ))
}

median_of() {   # median of a newline list of integers on stdin
    sort -n | awk '{a[NR]=$1} END{ if(NR==0){print 0; exit} m=int((NR+1)/2); if(NR%2){print a[m]} else {print int((a[m]+a[m+1])/2)} }'
}

# ---- Local backup check (one dir) ----
# Args: <label> <dir> <glob> <min_bytes>
check_local() {
    local label="$1" dir="$2" glob="$3" min="$4"
    if [ ! -d "$dir" ]; then
        fail "${label}: backup dir missing: ${dir}"
        log  "${label}: checked 0 of >=1 expected (dir missing)"
        return
    fi

    # Collect files newest-first with sizes.
    mapfile -t files < <(find "$dir" -maxdepth 1 -name "$glob" -type f -printf '%T@ %s %p\n' 2>/dev/null | sort -nr)
    local n="${#files[@]}"
    if [ "$n" -eq 0 ]; then
        fail "${label}: ZERO backup files matching ${glob} in ${dir} (vacuous-pass guard fired)"
        log  "${label}: checked 0 of >=1 expected (empty dir)"
        return
    fi

    local newest_line newest_size newest_path age
    newest_line="${files[0]}"
    newest_size=$(awk '{print $2}' <<< "$newest_line")
    newest_path=$(awk '{print $3}' <<< "$newest_line")
    age=$(embedded_age_seconds "$(basename "$newest_path")")

    # Freshness (embedded stamp; fall back to mtime if unparseable).
    if [ -z "$age" ]; then
        age=$(( $(date +%s) - $(awk '{printf "%d", $1}' <<< "$newest_line") ))
        log "${label}: WARN could not parse embedded stamp from $(basename "$newest_path"); used mtime"
    fi
    if [ "$age" -gt "$MAX_AGE_SECONDS" ]; then
        fail "${label}: newest backup is STALE: $(basename "$newest_path") age=$(( age/3600 ))h (max $(( MAX_AGE_SECONDS/3600 ))h)"
    fi

    # Absolute floor.
    if [ "$newest_size" -lt "$min" ]; then
        fail "${label}: newest backup UNDERSIZE vs floor: $(basename "$newest_path") ${newest_size}B < ${min}B"
    fi

    # Integrity: both chroma (.tar.gz) and mongo (.archive.gz) are gzip streams.
    # gzip -t catches a present-but-corrupt/truncated backup deterministically —
    # this is the sound replacement for the fragile "start-without-complete" log
    # heuristic, and directly covers the 2026-08-04 silent-failure shape.
    if ! gzip -t "$newest_path" 2>/dev/null; then
        fail "${label}: gzip integrity FAILED on $(basename "$newest_path") — corrupt/truncated backup"
    fi

    # Trailing-median undersize (skip newest itself; needs >=3 priors to be meaningful).
    if [ "$n" -ge 4 ]; then
        local median frac
        median=$(printf '%s\n' "${files[@]:1:$MEDIAN_WINDOW}" | awk '{print $2}' | median_of)
        if [ "$median" -gt 0 ]; then
            frac=$(( newest_size * 100 / median ))
            if [ "$frac" -lt "$UNDERSIZE_FRACTION_PCT" ]; then
                fail "${label}: newest backup ${frac}% of trailing median (${newest_size}B vs ${median}B) — suspected truncation"
            fi
        fi
    fi

    log "${label}: checked ${n} of ${n} present; newest $(basename "$newest_path") ${newest_size}B age=$(( age/3600 ))h — $( [ "$FAILURES" -eq 0 ] && echo OK || echo 'see failures above')"
}

# ---- Storm freshness (offsite copy) ----
# Args: <label> <win_subdir> <glob>
check_storm() {
    local label="$1" subdir="$2" glob="$3"
    if [ ! -e "$STORM_KEY" ]; then
        fail "storm/${label}: ssh key missing ${STORM_KEY} — cannot verify offsite copy"
        return
    fi
    local newest
    newest=$(ssh $STORM_SSH_OPTS "${STORM_USER}@${STORM_HOST}" \
        "dir /b /o-d \"${STORM_DIR_WIN}\\${subdir}\\${glob}\" 2>nul" 2>/dev/null | tr -d '\r' | head -1)
    if [ -z "$newest" ]; then
        fail "storm/${label}: no ${glob} in ${STORM_DIR_WIN}\\${subdir} (or storm unreachable) — offsite copy UNKNOWN, treated as FAIL"
        return
    fi
    local age
    age=$(embedded_age_seconds "$newest")
    if [ -z "$age" ]; then
        fail "storm/${label}: could not parse date from newest offsite file '${newest}'"
        return
    fi
    if [ "$age" -gt "$MAX_AGE_SECONDS" ]; then
        fail "storm/${label}: offsite copy STALE: ${newest} age=$(( age/3600 ))h (max $(( MAX_AGE_SECONDS/3600 ))h)"
    else
        log "storm/${label}: offsite OK: ${newest} age=$(( age/3600 ))h"
    fi
}

# ---- Supplementary: log corroboration (ADVISORY ONLY — never alerts) ----
# Both backup wrappers write "Backup complete: <path-with-YYYYMMDD>" on success.
# We look for a success line referencing TODAY's filename date (YYYYMMDD, the
# unambiguous form — the ISO log timestamps use dashes and do NOT appear on the
# success line). This is a human-facing NOTE in the journal only; it NEVER
# increments FAILURES. Rationale: the load-bearing signal is presence + freshness
# + size + gzip integrity above; /tmp clears on reboot and log formats drift, so
# a log-based check must not be allowed to false-alert Tom daily.
check_log_signature() {
    local label="$1" logf="$2"
    [ -f "$logf" ] || { log "${label}: (advisory) ${logf} absent (reboot since run?) — log corroboration skipped"; return; }
    local fdate
    fdate=$(date +%Y%m%d)
    if grep "Backup complete" "$logf" 2>/dev/null | grep -q "$fdate"; then
        log "${label}: (advisory) log shows 'Backup complete' for ${fdate} — corroborated"
    else
        log "${label}: (advisory) no 'Backup complete' line for ${fdate} in log (may be rotated/pre-reboot); relying on file+integrity checks, which are load-bearing"
    fi
}

# ================= run all checks =================
log "=== sage junto-backup verification start ==="

check_local  "chroma-local" "$CHROMA_DIR" "$CHROMA_GLOB" "$CHROMA_MIN_BYTES"
check_local  "mongo-local"  "$MONGO_DIR"  "$MONGO_GLOB"  "$MONGO_MIN_BYTES"
check_storm  "chroma"       "chroma"      "chroma-backup-*.tar.gz"
check_storm  "mongo"        "mongo"       "mongo-backup-*.archive.gz"
check_log_signature "chroma-log" "$CHROMA_LOG"
check_log_signature "mongo-log"  "$MONGO_LOG"

if [ "$FAILURES" -gt 0 ]; then
    err "=== VERIFICATION FAILED: ${FAILURES} fault(s) — see above ==="
    exit 1
fi
log "=== VERIFICATION PASS: local (chroma+mongo) fresh & sized, offsite (storm) fresh ==="
exit 0
