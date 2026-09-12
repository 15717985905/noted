#!/usr/bin/env bash
# scripts/install.sh — Install noted LaunchAgent
set -euo pipefail

NOTES_DIR_DEFAULT="${HOME}/ai-notes"
PORT_DEFAULT=8765
PYTHON="$(command -v python3)"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_DEST="${LAUNCH_AGENTS_DIR}/com.noted.plist"
TEMPLATE="$(cd "$(dirname "$0")" && pwd)/../assets/com.noted.plist.template"
LOG_DIR="${HOME}/Library/Logs"
LOG_PATH="${LOG_DIR}/noted.log"

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Install noted LaunchAgent for auto-start on login.

Options:
  --notes-dir DIR    Notes directory (default: ~/ai-notes)
  --port PORT        Server port (default: 8765)
  --python PATH      Python executable (default: $(command -v python3))
  -h, --help         Show this help
EOF
    exit 0
}

NOTES_DIR="${NOTES_DIR_DEFAULT}"
PORT="${PORT_DEFAULT}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --notes-dir)
            NOTES_DIR="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --python)
            PYTHON="$2"
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

if [[ -z "${PYTHON}" || ! -x "${PYTHON}" ]]; then
    echo "Error: python3 not found" >&2
    exit 1
fi

mkdir -p "${LAUNCH_AGENTS_DIR}" "${LOG_DIR}"
mkdir -p "${NOTES_DIR}"

python3 -c "
import sys, os
template = open(sys.argv[1], 'r').read()
rendered = template.replace('{{PYTHON}}', sys.argv[2])
rendered = rendered.replace('{{LOG_PATH}}', sys.argv[3])
rendered = rendered.replace('{{NOTES_DIR}}', sys.argv[4])
rendered = rendered.replace('{{PORT}}', sys.argv[5])
open(sys.argv[6], 'w').write(rendered)
" "${TEMPLATE}" "${PYTHON}" "${LOG_PATH}" "${NOTES_DIR}" "${PORT}" "${PLIST_DEST}"

launchctl unload "${PLIST_DEST}" 2>/dev/null || true
cp "${PLIST_DEST}" "${PLIST_DEST}.bak" 2>/dev/null || true
launchctl load "${PLIST_DEST}"

echo "Installed LaunchAgent: ${PLIST_DEST}"
echo "Notes dir: ${NOTES_DIR}"
echo "Port: ${PORT}"
echo "Python: ${PYTHON}"
echo "Log: ${LOG_PATH}"
