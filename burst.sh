#!/usr/bin/env bash
# Usage: ADMIN_TOKEN=<token> ./burst.sh <BASE_URL> [extra options, see --help]
set -euo pipefail

BASE_URL="${1:-${BASE_URL:-}}"
if [ -z "$BASE_URL" ]; then
  echo "usage: ADMIN_TOKEN=<token> ./burst.sh <BASE_URL> [options]" >&2
  exit 2
fi
shift || true

cd "$(dirname "$0")"

if [ ! -x .venv-burst/bin/python ]; then
  python3 -m venv .venv-burst
  .venv-burst/bin/pip install --quiet aiohttp
fi

exec .venv-burst/bin/python scripts/burst.py --base-url "$BASE_URL" "$@"
