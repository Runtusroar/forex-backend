#!/usr/bin/env bash
set -euo pipefail

: "${CHROME_BINARY:?Set CHROME_BINARY to the Chrome or Chromium executable}"
: "${CHROME_PROFILE_DIR:?Set CHROME_PROFILE_DIR to a dedicated absolute profile directory}"

case "$CHROME_PROFILE_DIR" in
  /|"")
    echo "CHROME_PROFILE_DIR must be a dedicated directory" >&2
    exit 2
    ;;
esac

mkdir -p "$CHROME_PROFILE_DIR"
chmod 700 "$CHROME_PROFILE_DIR"

remote_debugging_address="${CHROME_REMOTE_DEBUGGING_ADDRESS:-127.0.0.1}"
chrome_args=(
  "--remote-debugging-address=${remote_debugging_address}"
  "--remote-debugging-port=9222"
  "--user-data-dir=${CHROME_PROFILE_DIR}"
  "--no-first-run"
  "--no-default-browser-check"
)

if [[ "${CHROME_CONTAINER_MODE:-0}" == "1" ]]; then
  chrome_args+=("--no-sandbox" "--disable-dev-shm-usage")
fi

exec "$CHROME_BINARY" "${chrome_args[@]}"
