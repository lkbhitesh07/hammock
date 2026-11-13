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
    "$0" status
    cat >&2 <<USAGE

Nothing changed. Usage:
  $0 1        plant bug 1 (pricing renames 'total' to 'total_amount'),
              then: hammock restart pricing && ./order.sh
  $0 reset    remove all bugs
  $0 status   show which bugs are planted
USAGE
    exit 2
    ;;
esac
