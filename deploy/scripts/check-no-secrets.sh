#!/usr/bin/env bash
# Fail if any given secret occurs in a log file (FR-017). Prints only the 1-based index of a
# matching secret, never the secret itself.
#
# usage: check-no-secrets.sh <logfile> <secret>...
# exit:  0 no secret found, 1 a secret was found, 2 usage error or unreadable log
set -euo pipefail

if [ "$#" -lt 2 ]; then
    echo "usage: check-no-secrets.sh <logfile> <secret>..." >&2
    exit 2
fi

log="$1"
shift
if [ ! -f "$log" ]; then
    echo "error: $log is not a file" >&2
    exit 2
fi

has_secret=0
for secret in "$@"; do
    if [ -n "$secret" ]; then
        has_secret=1
    fi
done
if [ "$has_secret" -eq 0 ]; then
    echo "usage: at least one non-empty secret is required (all arguments are empty)" >&2
    exit 2
fi

found=0
index=0
for secret in "$@"; do
    index=$((index + 1))
    if [ -z "$secret" ]; then
        continue
    fi
    # grep exits 2 for a read error; that must not count as "not found".
    rc=0
    grep -qF -e "$secret" "$log" || rc=$?
    if [ "$rc" -eq 0 ]; then
        echo "secret #$index appears in $log" >&2
        found=1
    elif [ "$rc" -gt 1 ]; then
        echo "error: cannot read $log" >&2
        exit 2
    fi
done
exit "$found"
