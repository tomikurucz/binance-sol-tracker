#!/usr/bin/env bash
#
# Orankenti futtatas systemd timerrel (a GitHub Actions kivaltasa VPS-en).
#
# Hasznalat a repo gyokerebol:
#     sudo bash deploy/install-scheduler.sh
#
# A futtato felhasznalo a sudo-t indito felhasznalo (vagy SOL_TRACKER_USER=...),
# az utvonal pedig automatikusan ez a klon (barmelyik mappabol mukodik).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SOL_TRACKER_USER:-${SUDO_USER:-$(id -un)}}"

if [[ $EUID -ne 0 ]]; then
    echo "HIBA: root (sudo) kell: sudo bash deploy/install-scheduler.sh" >&2
    exit 1
fi

if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    echo "HIBA: nincs $APP_DIR/.venv/bin/python" >&2
    echo "      Hozd letre elobb: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi
if [[ ! -f "$APP_DIR/.env" ]]; then
    echo "HIBA: nincs $APP_DIR/.env (cp .env.example .env, majd toltsd ki)." >&2
    exit 1
fi
mkdir -p "$APP_DIR/logs"

render() {
    sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@RUN_USER@|$RUN_USER|g" "$1"
}

render "$APP_DIR/deploy/sol-tracker.service" | tee /etc/systemd/system/sol-tracker.service >/dev/null
install -m 644 "$APP_DIR/deploy/sol-tracker.timer" /etc/systemd/system/sol-tracker.timer
render "$APP_DIR/deploy/logrotate.conf" | tee /etc/logrotate.d/sol-tracker >/dev/null

systemctl daemon-reload
systemctl enable --now sol-tracker.timer

echo
echo "Telepitve: $APP_DIR (felhasznalo: $RUN_USER)"
systemctl list-timers sol-tracker.timer --no-pager
echo
echo "Kovetkezo futas a lista tetejen. Proba most:"
echo "    sudo systemctl start sol-tracker.service"
echo "    tail -n 30 $APP_DIR/logs/signals.log"
