#!/usr/bin/env bash
# Developer CLI helper for Fincas
set -euo pipefail

COMMAND="${1:-help}"
PYTHON="${PYTHON:-$(if [ -f .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)}"

function show_help() {
    cat <<EOF
Fincas Development Helper

Usage: ./scripts/dev.sh [command]

Commands:
  up          Start Docker containers (PostgreSQL, Valkey/Redis, Django)
  down        Stop Docker containers
  logs        Follow web container logs
  test        Run local Django test suite
  migrate     Run database migrations
  seed        Seed database with demo products, prices, warehouse stock & accounts
  shell       Open interactive Django shell
  tripwire    Run repository isolation check
  check       Run Django health and migration checks
EOF
}

case "$COMMAND" in
    up)
        docker compose up -d
        ;;
    down)
        docker compose down
        ;;
    logs)
        docker compose logs -f web
        ;;
    test)
        $PYTHON manage.py test --settings=config.settings.test "${@:2}"
        ;;
    migrate)
        $PYTHON manage.py migrate "${@:2}"
        ;;
    seed)
        $PYTHON manage.py seed_demo_data "${@:2}"
        ;;
    shell)
        $PYTHON manage.py shell
        ;;
    tripwire)
        bash scripts/tripwire.sh
        ;;
    check)
        $PYTHON manage.py check
        $PYTHON manage.py makemigrations --check
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        echo "Unknown command: $COMMAND"
        show_help
        exit 1
        ;;
esac
