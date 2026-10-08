#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_DIR="$PROJECT_DIR/deploy/local"

if [[ ! -f "$LOCAL_DIR/moss-note.sudoers" ]]; then
  echo "Run scripts/restore-migration.py first." >&2
  exit 1
fi

sudo /usr/sbin/visudo -cf "$LOCAL_DIR/moss-note.sudoers"
sudo install -o root -g root -m 440 "$LOCAL_DIR/moss-note.sudoers" /etc/sudoers.d/moss-note
for name in app model fan; do
  sudo install -o root -g root -m 644 "$LOCAL_DIR/moss-note-$name.service" /etc/systemd/system/
done
sudo systemctl daemon-reload
echo "Installed app/model/fan units without starting or enabling them. Tunnel is not installed."
