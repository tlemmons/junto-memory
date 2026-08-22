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

echo "[deploy] installing check script -> ${SBIN}/junto-backup-verify.sh"
sudo install -m 0755 -o root -g root "${HERE}/junto-backup-verify.sh" "${SBIN}/junto-backup-verify.sh"

echo "[deploy] installing systemd units -> ${UNITDIR}"
for u in junto-backup-verify.service junto-backup-verify.timer junto-backup-verify-alert.service; do
    sudo install -m 0644 -o root -g root "${HERE}/${u}" "${UNITDIR}/${u}"
done

echo "[deploy] reloading systemd + enabling timer"
sudo systemctl daemon-reload
sudo systemctl enable --now junto-backup-verify.timer

echo "[deploy] msmtprc check"
if [ ! -f /home/tlemmons/.msmtprc ]; then
    echo "  !! /home/tlemmons/.msmtprc MISSING — install it from msmtprc.example (chmod 600). Alerts will NOT send until you do."
else
    echo "  ok: /home/tlemmons/.msmtprc present"
fi

echo "[deploy] status:"
systemctl status junto-backup-verify.timer --no-pager || true
echo
echo "[deploy] done. Dry-run the check now with:  sudo systemctl start junto-backup-verify.service && journalctl -u junto-backup-verify.service -n 40 --no-pager"
echo "[deploy] fail-test the alert path with a forced failure (e.g. temporarily point CHROMA_DIR at an empty dir) and confirm email delivery."
