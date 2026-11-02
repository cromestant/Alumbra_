#!/usr/bin/env bash
# Alumbra — VPS deploy update. Run on the VPS from the checkout the
# systemd unit points at (its WorkingDirectory's parent):
#   /srv/alumbra/deploy/deploy-update.sh
#
# Pulls the latest committed code, refreshes Python dependencies,
# restarts the service, and proves the new build answers /health.
# Secrets are never touched: .env is server-local state, outside git.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# The port the service binds: the unit reads ALUMBRA_PORT from this
# .env (EnvironmentFile=), so the health check reads the same value.
PORT="$(sed -n 's/^ALUMBRA_PORT=//p' .env 2>/dev/null | tail -1 | tr -d "\"' ")"
if [ -z "$PORT" ]; then
  echo "ALUMBRA_PORT is not set in $ROOT/.env (the unit needs it too)" >&2
  exit 1
fi

PREV="$(git rev-parse HEAD)"
git pull --ff-only
echo "deploying: $(git log --oneline -1)  (was ${PREV:0:7})"

# The venv is created once; recreating it here only if it's missing.
[ -d .venv ] || python3 -m venv .venv
# Cheap enough to run every deploy; a no-op unless requirements moved.
.venv/bin/pip install -q -r backend/requirements.txt

# The public site (deploy/alumbra-nginx.conf serves /var/www/alumbra).
# download/ holds the APK, uploaded by hand, so the sync never touches it.
SITE=/var/www/alumbra
if [ -d "$SITE" ]; then
  rsync -a --delete --exclude /download/ site/ "$SITE/"
  echo "site synced to $SITE"
else
  echo "no $SITE: site not synced (see deploy/alumbra-nginx.conf)"
fi

sudo systemctl restart alumbra-api

for i in $(seq 1 15); do
  if curl -sf "http://127.0.0.1:${PORT}/health" > /dev/null; then
    echo "health OK on :${PORT} — $(git rev-parse --short HEAD) is live"
    exit 0
  fi
  sleep 1
done

echo "HEALTH CHECK FAILED after restart. Service log: journalctl -u alumbra-api -n 50" >&2
# reset, not checkout: the branch stays checked out, so the next
# deploy's pull --ff-only still works. .env is outside git, untouched.
echo "Rollback: git reset --hard ${PREV:0:7} && sudo systemctl restart alumbra-api" >&2
exit 1
