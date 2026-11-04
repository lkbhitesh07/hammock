#!/usr/bin/env bash
# Plant or remove the demo's bugs.
#
# Bugs are real source edits, not env flags, so they show up in `git diff` exactly
# like a teammate's regression would.
set -euo pipefail
cd "$(dirname "$0")"

pricing=pricing/src/main.rs

case "${1:-}" in
  1)
    # Rename the field in the pricing response; the Django consumer still reads `total`.
    sed -i 's/\btotal:/total_amount:/g' "$pricing"
    echo "bug 1 planted: pricing now returns 'total_amount' instead of 'total'"
    ;;
  reset)
    sed -i 's/\btotal_amount:/total:/g' "$pricing"
    echo "all bugs removed"
    ;;
  status)
    if grep -q 'total_amount:' "$pricing"; then echo "bug 1: planted"; else echo "bug 1: off"; fi
    ;;
  *)
    echo "usage: $0 1 | reset | status" >&2
    exit 2
    ;;
esac
