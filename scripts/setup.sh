#!/usr/bin/env bash
# Bootstrap the signal-agent directory structure and Python deps.
# Idempotent: safe to re-run.

set -euo pipefail

AGENT_DATA="${AGENT_DATA:-$HOME/workspace/agent-data}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

echo "[setup] Creating directory structure under $AGENT_DATA"
mkdir -p "$AGENT_DATA"/{notes,calendar-game,mail-work,mail-personal,state,logs}
mkdir -p "$AGENT_DATA"/archives/{work-mail,work-cloud,work-hr,work-misc}

echo "[setup] Installing Python dependencies"
python3 -m pip install --user -r "$REPO_DIR/requirements.txt"

echo "[setup] Done."
echo
echo "Next steps:"
echo "  1. Grant Automation permission when macOS prompts on first notes dump."
echo "  2. Read docs/exporting-work-mail.md and start the one-time .olm export."
echo "  3. Configure each dumper (see scripts/*.example.json files for templates)."
echo "  4. Run each dumper once manually to verify, then install launchd jobs."
