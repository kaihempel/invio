#!/usr/bin/env bash
# Verify the shipped systemd units with systemd-analyze (SC-001): exit 0 with no output.
#
# usage: verify-units.sh [--if-available]
#   --if-available  skip (exit 0, notice on stderr) when systemd-analyze is missing, for
#                   pre-commit on macOS. CI never passes this flag.
# exit:  0 clean, 1 systemd-analyze failed or printed anything, 2 usage/environment error
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$script_dir/../.." && pwd)"
units_dir="$repo/deploy/systemd"

if_available=0
case "${1:-}" in
    "") ;;
    --if-available) if_available=1 ;;
    *)
        echo "usage: verify-units.sh [--if-available]" >&2
        exit 2
        ;;
esac

if ! command -v systemd-analyze >/dev/null 2>&1; then
    if [ "$if_available" -eq 1 ]; then
        echo "skipped: systemd-analyze not found" >&2
        exit 0
    fi
    echo "error: systemd-analyze not found" >&2
    exit 2
fi

shopt -s nullglob
units=("$units_dir"/*.service "$units_dir"/*.timer)
if [ "${#units[@]}" -eq 0 ]; then
    echo "error: no units found in $units_dir" >&2
    exit 2
fi

root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT

# systemd-analyze verify checks that ExecStart exists, so provide a stub invio binary.
mkdir -p "$root/opt/invio/.venv/bin" "$root/etc/systemd/system"
printf '#!/bin/sh\nexit 0\n' >"$root/opt/invio/.venv/bin/invio"
chmod 0755 "$root/opt/invio/.venv/bin/invio"
cp "${units[@]}" "$root/etc/systemd/system/"

# With --root, systemd-analyze only sees units below the root, so the targets the units order
# against (sysinit.target, network-online.target, ...) must exist there too.
mkdir -p "$root/usr/lib/systemd/system"
for dir in /usr/lib/systemd/system /lib/systemd/system; do
    if [ -d "$dir" ]; then
        cp "$dir"/*.target "$root/usr/lib/systemd/system/"
        break
    fi
done

targets=("$root"/etc/systemd/system/invio-*)
status=0
output="$(systemd-analyze verify --root="$root" "${targets[@]}" 2>&1)" || status=$?
if [ "$status" -ne 0 ] || [ -n "$output" ]; then
    if [ -n "$output" ]; then
        printf '%s\n' "$output"
    fi
    # Propagate systemd-analyze's own exit code; any output without a failure is still exit 1.
    exit "$((status != 0 ? status : 1))"
fi
