#!/usr/bin/env bash
# scripts/smoke.sh — End-to-end smoke test for noted
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON="$(command -v python3)"
NOTES_DIR="$(mktemp -d)"
PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
PID=""
PASS=0
FAIL=0

cleanup() {
    if [[ -n "${PID}" ]] && kill -0 "${PID}" 2>/dev/null; then
        kill "${PID}" 2>/dev/null || true
        wait "${PID}" 2>/dev/null || true
    fi
    rm -rf "${NOTES_DIR}"
}
trap cleanup EXIT

export NOTED_HOME="${NOTES_DIR}"
export NOTED_PORT="${PORT}"
export PYTHONPATH="${REPO_ROOT}/src"

echo "Starting noted server on port ${PORT} with notes dir ${NOTES_DIR}..."
cd "${REPO_ROOT}"
"${PYTHON}" -m noted.hub &
PID=$!
sleep 1
base="http://127.0.0.1:${PORT}"

echo "# Star Test" > "${NOTES_DIR}/star.md"
echo "tags: test" >> "${NOTES_DIR}/star.md"
echo "" >> "${NOTES_DIR}/star.md"
echo "body" >> "${NOTES_DIR}/star.md"

check() {
    local name="$1"
    local expected="$2"
    local actual="$3"
    if [[ "${actual}" == "${expected}" ]]; then
        echo "  PASS: ${name}"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: ${name} (expected ${expected}, got ${actual})"
        FAIL=$((FAIL + 1))
    fi
}

echo "GET /"
status=$(curl -s -o /dev/null -w "%{http_code}" "${base}/")
check "GET /" "200" "${status}"

echo "GET /api/list"
status=$(curl -s -o /dev/null -w "%{http_code}" "${base}/api/list")
check "GET /api/list" "200" "${status}"

echo "POST /api/star"
status=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST \
    -H "Host: localhost:${PORT}" \
    -H "Origin: http://localhost:${PORT}" \
    -H "Content-Type: application/json" \
    -d '{"file":"star.md","starred":true}' \
    "${base}/api/star")
check "POST /api/star (same origin)" "200" "${status}"

echo "GET /api/read/html"
echo -e "# Hello\n\nbody" > "${NOTES_DIR}/hello.md"
body=$(curl -s "${base}/api/read/html?file=hello.md")
check "GET /api/read/html contains h1" "1" "$(echo "${body}" | grep -c '<h1>' || true)"

echo "GET /api/list with malicious Host"
status=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Host: evil.com" \
    "${base}/api/list")
check "GET /api/list malicious Host 403" "403" "${status}"

echo "GET /api/discover"
status=$(curl -s -o /dev/null -w "%{http_code}" "${base}/api/discover")
check "GET /api/discover" "200" "${status}"
body=$(curl -s "${base}/api/discover")
echo "${body}" | grep -q "candidate_id" || true
check "GET /api/discover no absolute path" "0" "$(echo "${body}" | grep -c '\/Users\/' || true)"

echo ""
echo "================================"
if [[ "${FAIL}" -eq 0 ]]; then
    echo "PASS (${PASS} checks passed)"
    exit 0
else
    echo "FAIL (${PASS} passed, ${FAIL} failed)"
    exit 1
fi
