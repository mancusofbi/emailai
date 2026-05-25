#!/usr/bin/env bash
# Check gws auth status; log in if the token isn't valid.
set -e

status_json="$(gws auth status 2>/dev/null | sed -n '/^{/,/^}/p')"
token_valid="$(echo "$status_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token_valid", False))' 2>/dev/null || echo False)"

if [ "$token_valid" = "True" ]; then
    echo "Authenticated. Token valid."
    gws gmail users getProfile --params '{"userId": "me"}' 2>/dev/null \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); print("Account:", d.get("emailAddress"))' \
        || true
else
    echo "Token not valid. Launching gws auth login..."
    # Pipe through a line-reader that auto-opens the OAuth URL in the browser.
    # gws prints the URL and then blocks listening on localhost for the callback,
    # so opening as soon as we see the line is safe.
    gws auth login 2>&1 | while IFS= read -r line; do
        echo "$line"
        if [[ "$line" == *"https://accounts.google.com"* ]]; then
            url=$(printf '%s\n' "$line" | grep -oE 'https://accounts\.google\.com[^[:space:]]+')
            [ -n "$url" ] && open "$url"
        fi
    done
fi
