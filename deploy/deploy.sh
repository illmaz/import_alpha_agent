#!/usr/bin/env bash
#
# Deploy the current main branch to the VPS. Run from your laptop:
#
#   DEPLOY_HOST=deploy@api.example.com ./deploy/deploy.sh
#
# Pulls on the server, rebuilds, runs migrations, restarts, and does not
# return success unless /health answers afterwards.
#
# Environment:
#   DEPLOY_HOST   user@host (required)
#   APP_DIR       path on the server (default /opt/importalpha)
#   BRANCH        branch to deploy   (default main)
#   SSH_OPTS      extra ssh flags, e.g. "-i ~/.ssh/id_ed25519 -p 2222"

set -euo pipefail

DEPLOY_HOST="${DEPLOY_HOST:-${1:-}}"
APP_DIR="${APP_DIR:-/opt/importalpha}"
BRANCH="${BRANCH:-main}"
SSH_OPTS="${SSH_OPTS:-}"
COMPOSE="docker compose -f deploy/docker-compose.prod.yml"

if [ -z "$DEPLOY_HOST" ]; then
  echo "usage: DEPLOY_HOST=user@host $0" >&2
  exit 1
fi

log() { printf '\n==> %s\n' "$1"; }

# Refuse to deploy a dirty tree. What is on the server should be a commit you
# can name, not whatever happened to be on someone's laptop.
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "error: working tree has uncommitted changes. Commit or stash first." >&2
  git status --short >&2
  exit 1
fi

LOCAL_SHA="$(git rev-parse --short HEAD)"
log "Deploying $BRANCH ($LOCAL_SHA) to $DEPLOY_HOST:$APP_DIR"

# shellcheck disable=SC2086 # SSH_OPTS is intentionally word-split
ssh $SSH_OPTS "$DEPLOY_HOST" APP_DIR="$APP_DIR" BRANCH="$BRANCH" 'bash -seuo pipefail' <<'REMOTE'
log() { printf '\n--> %s\n' "$1"; }

cd "$APP_DIR"

if [ ! -f .env ]; then
  echo "error: $APP_DIR/.env is missing. See docs/DEPLOYMENT.md." >&2
  exit 1
fi

COMPOSE="docker compose -f deploy/docker-compose.prod.yml"

log "Fetching $BRANCH"
git fetch --quiet origin "$BRANCH"
git checkout --quiet "$BRANCH"
git reset --hard --quiet "origin/$BRANCH"
echo "    now at $(git rev-parse --short HEAD) — $(git log -1 --pretty=%s)"

log "Backing up the database"
# Cheap insurance before migrations. Keeps the last 10.
if [ -f data/reports.db ]; then
  mkdir -p backups
  cp data/reports.db "backups/reports.$(date -u +%Y%m%dT%H%M%SZ).db"
  ls -1t backups/reports.*.db | tail -n +11 | xargs -r rm --
fi

log "Building"
$COMPOSE build

log "Running migrations"
# --no-deps: alembic needs the database file, not Kafka.
$COMPOSE run --rm --no-deps fastapi alembic upgrade head

log "Starting services"
$COMPOSE up -d --remove-orphans

log "Pruning dangling images"
docker image prune -f >/dev/null

log "Waiting for health"
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
    curl -sS http://127.0.0.1:8000/health
    echo
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "error: /health did not answer within 30s" >&2
    $COMPOSE ps
    $COMPOSE logs --tail 50 fastapi >&2
    exit 1
  fi
  sleep 1
done

log "Service status"
$COMPOSE ps
REMOTE

log "Deployed $LOCAL_SHA. Verify: https://<your-domain>/health"
