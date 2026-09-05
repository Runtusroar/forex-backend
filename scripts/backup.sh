#!/usr/bin/env bash
# Linux host: consistent database + referenced media + diagnostic snapshots.
set -euo pipefail
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
backup_dir=${1:-"$repo_dir/backups"}
mkdir -p -- "$backup_dir"
backup_dir=$(cd -- "$backup_dir" && pwd)
exec 9>"$backup_dir/.backup.lock"
flock -n 9 || exit 0
cd -- "$repo_dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
name="daily-$stamp"
container_path="/tmp/forex-$name"
staging="$backup_dir/.$name"
cleanup() {
  docker compose exec -T api rm -rf -- "$container_path" >/dev/null 2>&1 || true
  if [[ -d "$staging" ]]; then rm -rf -- "$staging"; fi
}
trap cleanup EXIT
docker compose exec -T api python -m app.maintenance backup \
  --database /app/data/forex_factory.sqlite3 --media-dir /app/data/media \
  --snapshot-dir /app/data/snapshots --output "$container_path"
docker compose cp "api:$container_path" "$staging"
python3 app/maintenance.py verify "$staging"
mv -- "$staging" "$backup_dir/$name"
# Only this script's named daily backups expire; pre-deployment backups remain.
find "$backup_dir" -maxdepth 1 -type d -name 'daily-20?????????????Z' -mtime +13 \
  -exec rm -rf -- {} +
printf 'Verified backup: %s\n' "$backup_dir/$name"
