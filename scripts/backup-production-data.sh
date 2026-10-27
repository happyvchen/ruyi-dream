#!/usr/bin/env bash
set -euo pipefail

# LILY single-host cold backup. Kept separate from release-backups retention.
data_root="${DATA_ROOT:-/data/ruyi-dream}"
env_file="${RUYI_ENV_FILE:-/home/lilyroot/ruyi-dream/.env}"
backup_root="${data_root}/periodic-data-backups"
mkdir -p "${backup_root}"
exec 9>"${backup_root}/.lock"
flock -n 9 || { echo "Another backup is running" >&2; exit 1; }

test -d "${data_root}/rustfs/data"
test -f "${env_file}"
docker inspect ruyi-dream-backend ruyi-dream-rustfs >/dev/null

docker exec -i ruyi-dream-backend python - <<'PY'
import sqlite3
conn = sqlite3.connect("file:/app/data/app.sqlite3?mode=ro", uri=True)
running = conn.execute("SELECT count(*) FROM generation_attempts WHERE status = 'RUNNING'").fetchone()[0]
uploads = conn.execute("SELECT count(*) FROM upload_sessions WHERE status IN ('PENDING', 'UPLOADING', 'UPLOADED', 'FINALIZING')").fetchone()[0]
conn.close()
if running or uploads:
    raise SystemExit(f"Backup deferred: running_attempts={running}, active_uploads={uploads}")
PY

if [[ "${1:-}" == "--check" ]]; then
  echo "Backup prerequisites OK; no active work"
  exit 0
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="${backup_root}/${stamp}"
mkdir -p "${target}"
restart_needed=1
restart_services() {
  if [[ "${restart_needed}" == 1 ]]; then
    docker start ruyi-dream-rustfs >/dev/null || true
    docker start ruyi-dream-backend >/dev/null || true
  fi
}
trap restart_services EXIT

docker stop ruyi-dream-backend ruyi-dream-rustfs >/dev/null
docker cp ruyi-dream-backend:/app/data "${target}/app-data"
cp -- "${env_file}" "${target}/prod.env"
chmod 600 "${target}/prod.env"
cp -R --reflink=always --preserve=mode,timestamps \
  "${data_root}/rustfs/data" "${target}/rustfs-data"

python3 - "${target}/app-data/app.sqlite3" <<'PY'
import sqlite3
import sys
conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
result = conn.execute("PRAGMA integrity_check").fetchone()[0]
conn.close()
if result != "ok":
    raise SystemExit(f"SQLite integrity_check failed: {result}")
PY

diffs="$(rsync -n -a --no-owner --no-group --delete --out-format='%i %n' \
  "${data_root}/rustfs/data/" "${target}/rustfs-data/")"
if [[ -n "${diffs}" ]]; then
  echo "RustFS copy differs from frozen source" >&2
  exit 1
fi

printf 'complete_at_utc=%s\n' "$(date -u +%FT%TZ)" >"${target}/COMPLETE"
restart_services
restart_needed=0
echo "Verified cold backup: ${target}"
