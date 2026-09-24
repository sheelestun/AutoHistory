#!/usr/bin/env bash
set -euo pipefail
umask 077
if [ "$#" -ne 3 ]; then
  echo 'Usage: bash ops/restore.sh dump.age private-age-key NEW_DATABASE_NAME' >&2
  exit 2
fi
dump=$1
key=$2
database=$3
if ! [[ "$database" =~ ^autohistory_restore_[a-zA-Z0-9_]+$ ]]; then
  echo 'Use a NEW database named autohistory_restore_<suffix>; existing databases are never overwritten.' >&2
  exit 2
fi
cd "$(dirname "$0")/.."
docker compose exec -T postgres createdb -U autohistory "$database"
age -d -i "$key" "$dump" | docker compose exec -T postgres pg_restore -U autohistory -d "$database" --no-owner --exit-on-error
echo "Restored $database. Before starting a bot: point DATABASE_URL at this database, replay the independent deletion journal with python -m app.jobs.replay_deletions, then run health and acceptance checks."
