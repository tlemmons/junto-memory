# junto-backup-verify — sage host-side backup monitor

Daily watchdog that alerts Tom by email if sage's **junto (shared-memory MCP)
backups** go missing, truncated, or stale. junto's persistence is MongoDB +
ChromaDB; these are the crown-jewel store for the whole fleet.

**Owner:** `memory@junto` (host-substrate for sage/junto).
**Runs on:** sage, as user `tlemmons`.
**Origin:** built + fail-tested by `infra-team@nimbus` on 2026-08-21 under
`backlog_d59e8fc1b760`; ownership moved to `memory@junto` on 2026-08-22 when Tom
decoupled sage/junto infra from the Nimbus project. Source was relocated here
from the nimbus infra repo at that point.

## Why it exists

On **2026-08-04** a chroma backup failed **silently** (a `tar` race on the live
SQLite file) and nothing alerted. `memory@junto` owns the backup jobs and the
tar-race root-cause fix; this is the durable **belt-and-suspenders** host-side
monitor that makes a silent failure impossible to miss.

## What it checks (daily 05:00 local, after the backup crons)

Backup crons on sage: chroma **03:00** → mongo **03:15** → sync-to-storm **04:00**.
Verify runs at **05:00** with an hour of slack.

For chroma (`~/chroma-backups`) and mongo (`~/mongo-backups`):
- **Presence** — a matching backup file exists (zero files = ALERT, never a
  vacuous pass; every run logs "checked N of N").
- **Freshness** — newest backup < **26h** old (embedded filename timestamp,
  mtime fallback).
- **Size floor** — chroma ≥ 50 MB, mongo ≥ 20 MB (deliberately well under
  observed ~200 MB / ~72 MB — catches truncation, not drift).
- **Trailing-median** — newest ≥ 50% of the trailing 7-file median (truncation).
- **gzip integrity** — `gzip -t` on the newest (catches present-but-corrupt;
  directly covers the 2026-08-04 shape).

Offsite (storm, Windows host `C:\SageBackup\{chroma,mongo}`, via
`~/.ssh/storm-backup`):
- **Freshness** — newest synced copy < 26h. **Storm unreachable = FAIL**, not a
  clean pass (offsite state UNKNOWN is treated as bad).

Log-signature corroboration is **advisory only** — it never alerts (`/tmp`
clears on reboot, log formats drift), so it can't false-alarm Tom daily.

## Alert path

On any failure the check exits non-zero → systemd `OnFailure=` fires
`junto-backup-verify-alert.service` → emails **tom@lemmons.net** via `msmtp`
(reads `/home/tlemmons/.msmtprc`). Fail-tested end-to-end 2026-08-21: forced
failure → email delivered, SMTP 250.

**Email is the load-bearing channel.** A junto-blocker MCP message was in the
original ask but v1 ships email-only — there's no solved shell→MCP send pattern
from a systemd context yet. Adding it is tracked follow-up (see below).

## Files

| File | Installed to | Notes |
|------|--------------|-------|
| `junto-backup-verify.sh` | `/usr/local/sbin/` | the check, `0755 root` |
| `junto-backup-verify.service` | `/etc/systemd/system/` | oneshot runner, `OnFailure=` alert |
| `junto-backup-verify.timer` | `/etc/systemd/system/` | daily 05:00 local, `Persistent=true` |
| `junto-backup-verify-alert.service` | `/etc/systemd/system/` | email-on-failure via msmtp |
| `msmtprc.example` | `~/.msmtprc` (by hand) | **template** — real file has the SMTP password, not in git |
| `deploy.sh` | — | idempotent installer |

## Deploy

```bash
./deploy.sh          # installs artifacts, daemon-reload, enables the timer
# first time only: install ~/.msmtprc from msmtprc.example (chmod 600)
# dry run:  sudo systemctl start junto-backup-verify.service && journalctl -u junto-backup-verify.service -n 40 --no-pager
```

## Known debt / follow-ups (owned by memory@junto)

1. **Own mail sender.** msmtp currently **reuses the Nimbus Zoho sender**
   (`dev@nimbusframe.net`) — hardcoded in `msmtprc` and in the `From:` header of
   the alert service. Tom wants junto to stand up its own sender for a clean
   break (deferred — no time to provision it yet). Tracked in the junto backlog.
2. **junto-blocker MCP channel** as a second alert channel alongside email.
3. **Stale `Documentation=` paths.** The three installed units still point
   `Documentation=` at the old nimbus Windows path
   (`file:///c/code/Nimbus/infra/sage/junto-backup/README.md`). The committed
   copies preserve that exactly so the repo mirrors what's live; fix on the next
   deploy to point here.
