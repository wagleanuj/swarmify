#!/bin/sh
# Usage: needs.sh VAR [VAR...] -- command [args...]
# Refuses to start the command while a named variable is empty, naming it. compose.yaml checks
# here, per service, because a `${VAR:?}` there would stop every service, even Kokoro alone.
missing=""
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
  eval "value=\${$1:-}"
  [ -n "$value" ] || missing="$missing $1"
  shift
done
[ "${1:-}" = "--" ] && shift
if [ -n "$missing" ]; then
  echo "missing in .env:$missing (CLAUDE_CODE_OAUTH_TOKEN comes from \`claude setup-token\`)" >&2
  exit 1
fi
exec "$@"
