#!/usr/bin/env bash
set -euo pipefail

# Wait for database readiness if DATABASE_URL is set to PostgreSQL
python - <<'EOF'
import os
import sys
import time
from urllib.parse import urlparse
import socket

db_url = os.environ.get("DATABASE_URL", "")
if db_url and ("postgres://" in db_url or "postgresql://" in db_url):
    url = urlparse(db_url)
    host = url.hostname or "localhost"
    port = url.port or 5432
    print(f"Waiting for database at {host}:{port}...", flush=True)
    start = time.time()
    while time.time() - start < 60:
        try:
            with socket.create_connection((host, port), timeout=2):
                print(f"Database at {host}:{port} is reachable.", flush=True)
                sys.exit(0)
        except OSError:
            time.sleep(1)
    print(f"Error: timed out waiting for database at {host}:{port}.", file=sys.stderr, flush=True)
    sys.exit(1)
EOF

# Automatically run migrations if AUTO_MIGRATE is enabled
if [ "${AUTO_MIGRATE:-0}" = "1" ] || [ "${AUTO_MIGRATE:-false}" = "true" ]; then
    echo "Running database migrations..."
    python manage.py migrate --noinput
fi

# Automatically collect static assets if COLLECT_STATIC is enabled
if [ "${COLLECT_STATIC:-0}" = "1" ] || [ "${COLLECT_STATIC:-false}" = "true" ]; then
    echo "Collecting static files..."
    python manage.py collectstatic --noinput --clear
fi

exec "$@"
