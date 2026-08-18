#!/usr/bin/env bash
# External watchdog for ls-lead-finder.service.
# Tries the health endpoint; if it is unreachable or not HTTP 200 with ok status,
# restarts the systemd user service. Should be invoked every 60s by
# ls-lead-finder-watchdog.timer.
set -euo pipefail

HEALTH_URL="http://127.0.0.1:8798/api/health"
SERVICE="ls-lead-finder.service"
LOG_TAG="ls-lead-finder-watchdog"

fail() {
    logger -t "$LOG_TAG" "Watchdog detected unhealthy service: $1"
    systemctl --user restart "$SERVICE"
    logger -t "$LOG_TAG" "Issued restart for $SERVICE"
}

# Check that curl can reach the endpoint and that the JSON status is ok.
if ! response=$(curl -sS --max-time 10 "$HEALTH_URL" 2>/dev/null); then
    fail "health endpoint unreachable"
    exit 1
fi

if ! printf '%s' "$response" | grep -q '"status"[: ]*"ok"'; then
    fail "health endpoint returned non-ok response: $response"
    exit 1
fi

# Optional: also ensure the systemd unit is actually active.
if ! systemctl --user is-active --quiet "$SERVICE"; then
    fail "service unit is not active"
    exit 1
fi

exit 0
