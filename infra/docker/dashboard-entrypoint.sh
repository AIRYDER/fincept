#!/bin/sh
# dashboard-entrypoint.sh — replace __NEXT_PUBLIC_API_URL__ placeholder in
# built JS files with the runtime NEXT_PUBLIC_API_URL env var.
#
# Next.js NEXT_PUBLIC_* vars are baked at build time.  To allow runtime
# override without rebuilding, we build with a placeholder and replace
# it here before starting server.js.
#
# This lets ECS inject the real API URL at task start time.

set -e

PLACEHOLDER="__NEXT_PUBLIC_API_URL__"
RUNTIME_URL="${NEXT_PUBLIC_API_URL:-http://localhost:8000/api}"

if [ "$RUNTIME_URL" = "$PLACEHOLDER" ]; then
    echo "WARNING: NEXT_PUBLIC_API_URL not set at runtime, using default"
    RUNTIME_URL="http://localhost:8000/api"
fi

echo "Replacing $PLACEHOLDER with $RUNTIME_URL in .next/static/"

# Replace the placeholder in all JS chunks.  Using find + sed is portable
# across Alpine (busybox) and Debian-based images.
find .next/static -type f -name '*.js' -exec \
    sed -i "s|${PLACEHOLDER}|${RUNTIME_URL}|g" {} +

# Also replace in the standalone server.js if it references the placeholder.
if [ -f server.js ]; then
    sed -i "s|${PLACEHOLDER}|${RUNTIME_URL}|g" server.js
fi

echo "Starting Next.js server with API_URL=$RUNTIME_URL"

# Execute the original CMD (node server.js)
exec "$@"
