#!/usr/bin/env bash
# Install the launchd jobs for the agent-data dumpers.
# Templates each plist with absolute paths, copies to ~/Library/LaunchAgents/,
# then loads them with launchctl. Idempotent: safe to re-run.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$LAUNCH_DIR"

LABELS=(
  "com.aubrey.agent.notes"
  "com.aubrey.agent.calendar"
  "com.aubrey.agent.mail"
  "com.aubrey.agent.gmail"
)

for label in "${LABELS[@]}"; do
  src="$REPO_DIR/launchd/$label.plist"
  dst="$LAUNCH_DIR/$label.plist"

  if [[ ! -f "$src" ]]; then
    echo "[install] missing $src, skipping"
    continue
  fi

  # Template the placeholders. Use | as sed delimiter since paths contain /.
  sed -e "s|__REPO__|$REPO_DIR|g" \
      -e "s|__HOME__|$HOME|g" \
      "$src" > "$dst"

  # Unload first if already loaded (ignore error on first install).
  launchctl unload "$dst" 2>/dev/null || true
  launchctl load "$dst"
  echo "[install] loaded $label"
done

echo
echo "Loaded jobs:"
launchctl list | grep "com.aubrey.agent" || true
echo
echo "To stop a job:    launchctl unload ~/Library/LaunchAgents/<label>.plist"
echo "To run once:      launchctl start <label>"
echo "Logs:             ~/agent-data/logs/"
