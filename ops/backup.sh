#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
source ops/backup.env
: "${AGE_RECIPIENT:?}" "${BACKUP_DEST:?}" "${DELETION_JOURNAL_HOST_DIR:?}"
mkdir -p backups/encrypted
stamp=$(date -u +%Y%m%dT%H%M%SZ)
target="backups/encrypted/AutoHistory_${stamp}.dump.age"
trap 'rm -f -- "$target.partial"' EXIT
docker compose exec -T postgres pg_dump -U autohistory -d autohistory -Fc \
  | age -r "$AGE_RECIPIENT" > "$target.partial"
test -s "$target.partial"
mv -- "$target.partial" "$target"
# A failed independent copy causes a non-zero exit and never records success.
rsync -a -- "$target" "$BACKUP_DEST"
# Journal must already be on independently durable storage, not just in this daily copy.
tar -C "$DELETION_JOURNAL_HOST_DIR" -cf - . | age -r "$AGE_RECIPIENT" > "backups/encrypted/deletions_${stamp}.tar.age"
rsync -a -- "backups/encrypted/deletions_${stamp}.tar.age" "$BACKUP_DEST"
date -u +%FT%TZ > backups/last-success
find backups/encrypted -maxdepth 1 -type f -name '*.age' -mtime +6 -delete
docker compose exec -T bot python -m app.jobs.cleanup
# Configure the same 7-day expiry on the independent destination.
