#!/usr/bin/env bash
#
# Install/refresh the junto-backup-verify monitor on sage.
# Idempotent: copies artifacts into place, reloads systemd, enables the timer.
# Run as a user with sudo on sage (the check + alert run as tlemmons).
#
# msmtprc is NOT installed by this script — it holds a secret. Install it by
# hand from msmtprc.example (chmod 600, owned by tlemmons) the first time.
#
# Ref: infra/sage/junto-backup/README.md
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SBIN=/usr/local/sbin
UNITDIR=/etc/systemd/system

echo "[deploy] installing check + alert scripts -> ${SBIN}/"
sudo install -m 0755 -o root -g root "${HERE}/junto-backup-verify.sh" "${SBIN}/junto-backup-verify.sh"
sudo install -m 0755 -o root -g root "${HERE}/junto-backup-verify-alert.sh" "${SBIN}/junto-backup-verify-alert.sh"

echo "[deploy] installing systemd units -> ${UNITDIR}"
for u in junto-backup-verify.service junto-backup-verify.timer junto-backup-verify-alert.service; do
    sudo install -m 0644 -o root -g root "${HERE}/${u}" "${UNITDIR}/${u}"
done

echo "[deploy] reloading systemd + enabling timer"
sudo systemctl daemon-reload
sudo systemctl enable --now junto-backup-verify.timer

echo "[deploy] alert-channel checks"
if [ -f /home/tlemmons/.config/sage-diskwatch/config ] && grep -q '^HA_WEBHOOK=' /home/tlemmons/.config/sage-diskwatch/config; then
    echo "  ok: HA_WEBHOOK present (primary alert channel — HA webhook, tag=junto-backup)"
else
    echo "  !! HA_WEBHOOK missing in ~/.config/sage-diskwatch/config — HA alert will be skipped."
fi
if [ -f /home/tlemmons/.msmtprc ]; then
    echo "  ok: /home/tlemmons/.msmtprc present (email fallback — transition-only, drop once HA phone-verified)"
else
    echo "  note: /home/tlemmons/.msmtprc absent — email fallback disabled (fine once HA is verified)."
fi

echo "[deploy] status:"
systemctl status junto-backup-verify.timer --no-pager || true
echo
echo "[deploy] done. Dry-run the check now with:  sudo systemctl start junto-backup-verify.service && journalctl -u junto-backup-verify.service -n 40 --no-pager"
echo "[deploy] fail-test the alert path with a forced failure (e.g. temporarily point CHROMA_DIR at an empty dir) and confirm email delivery."
