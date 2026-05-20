#!/bin/bash
# Wrapper for launchd — activates emailai gcloud config and runs triage.
# Rotates the launchd logs when they grow past LOG_MAX_BYTES so the error log
# can't grow unbounded from auth-expired periods.
export PATH="/Users/bothome/.pyenv/shims:/Users/bothome/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

LOG_DIR="$HOME/Library/Logs/emailai"
LOG_MAX_BYTES=$((5 * 1024 * 1024))   # 5 MB
EXIT_AUTH_EXPIRED=75

rotate() {
    local f="$1"
    [ -f "$f" ] || return 0
    local size
    size=$(stat -f%z "$f" 2>/dev/null || echo 0)
    if [ "$size" -gt "$LOG_MAX_BYTES" ]; then
        mv "$f" "$f.1"
    fi
}
rotate "$LOG_DIR/triage.log"
rotate "$LOG_DIR/triage.error.log"

gcloud config configurations activate emailai 2>/dev/null

python3 /Users/bothome/Projects/emailai/triage_inbox.py
status=$?
# Auth-expired is a known soft failure — exit 0 so launchd doesn't escalate.
if [ "$status" -eq "$EXIT_AUTH_EXPIRED" ]; then
    exit 0
fi
exit "$status"
