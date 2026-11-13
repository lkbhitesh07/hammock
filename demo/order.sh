#!/usr/bin/env bash
# Place one order against the demo API, the way the React shop will.
set -euo pipefail

request_id="web-$(head -c4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
body=$(mktemp)
trap 'rm -f "$body"' EXIT

echo "→ POST /api/orders  (X-Request-ID: $request_id)"
read -r status type < <(curl -sS --connect-timeout 3 -o "$body" -w '%{http_code} %{content_type}\n' \
  -H 'Content-Type: application/json' \
  -H "X-Request-ID: $request_id" \
  -d '{"items": [{"sku": "hammock-classic", "qty": 1}, {"sku": "sloth-plush", "qty": 2}]}' \
  "${API_URL:-http://127.0.0.1:8010}/api/orders")

if [[ $status == 000 ]]; then
  echo "← no answer: the API isn't reachable. Is \`hammock up\` running, and is api healthy? (\`hammock status\`)"
  exit 1
fi
echo "← HTTP $status"
if [[ $type == application/json* ]]; then
  cat "$body"; echo
else
  echo "  ($type, $(wc -c < "$body") bytes: Django's debug page; the traceback is in the api logs)"
fi
