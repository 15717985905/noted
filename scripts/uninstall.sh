#!/usr/bin/env bash
# scripts/uninstall.sh — Uninstall noted LaunchAgent
set -euo pipefail

PLIST="${HOME}/Library/LaunchAgents/com.noted.plist"

if [[ ! -f "${PLIST}" ]]; then
    echo "LaunchAgent not found: ${PLIST}" >&2
    exit 1
fi

launchctl unload "${PLIST}" 2>/dev/null || true
rm -f "${PLIST}" "${PLIST}.bak"

echo "Uninstalled LaunchAgent: ${PLIST}"
