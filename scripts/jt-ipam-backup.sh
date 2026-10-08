#!/bin/bash
# =============================================================================
# jt-ipam backup script
#
# Backup contents:
#   1. PostgreSQL: pg_dump -Fc (all data + alembic_version)
#   2. /etc/jt-ipam/backend.env       — SECRET_KEY/ENCRYPTION_KEY
#   3. /etc/jt-ipam/tls/              — self-signed certs (if TLS_MODE=direct)
#
# Run daily by jt-ipam-backup.timer. Retained for RETENTION_DAYS days; older ones auto-deleted.
#
# Every run, success or failure, writes $BACKUP_DIR/last-run (key=value lines, readable by
# the jtipam user) so the System diagnostics page can say whether backups work and why not.
# A run that fails used to leave only an empty dated directory and a line in the journal,
# which the backend cannot read: backups failed for weeks on a real site without anyone
# noticing.
#
# Security:
#   - Output files 0600; directories 0700
#   - The whole /var/backups/jt-ipam/ should be pushed off-site encrypted via ssh/s3
# =============================================================================

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/jt-ipam}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
ENV_FILE="${ENV_FILE:-/etc/jt-ipam/backend.env}"
TLS_DIR="${TLS_DIR:-/etc/jt-ipam/tls}"
STATUS_FILE="$BACKUP_DIR/last-run"
ERR_FILE=""
TARGET_DIR=""
DUMP_SIZE=""

# Previous successful run (kept when this one fails, so "last good backup" survives a failure)
LAST_SUCCESS=""
if [[ -r "$STATUS_FILE" ]]; then
    LAST_SUCCESS="$(sed -n 's/^last_success_at=//p' "$STATUS_FILE" | head -1)"
fi

write_status() {
    local status="$1" code="$2" now err=""
    now="$(date -Iseconds)"
    if [[ "$status" == "ok" ]]; then
        LAST_SUCCESS="$now"
    elif [[ -n "$ERR_FILE" && -s "$ERR_FILE" ]]; then
        # The last meaningful line from pg_dump etc., on one line (the file is key=value)
        err="$(grep -v '^\s*$' "$ERR_FILE" | grep -v '^pg_dump: detail:' | tail -1 | tr -d '\r' | cut -c1-500)"
    fi
    [[ -d "$BACKUP_DIR" ]] || return 0
    {
        echo "status=$status"
        echo "finished_at=$now"
        echo "exit_code=$code"
        echo "last_success_at=$LAST_SUCCESS"
        echo "dump_size=$DUMP_SIZE"
        echo "error=$err"
    } > "$STATUS_FILE.tmp"
    chown jtipam:jtipam "$STATUS_FILE.tmp" 2>/dev/null || true
    chmod 0640 "$STATUS_FILE.tmp"
    mv -f "$STATUS_FILE.tmp" "$STATUS_FILE"
}

on_exit() {
    local code=$?
    if [[ $code -ne 0 ]]; then
        if [[ -n "$ERR_FILE" ]] && grep -q 'permission denied for table' "$ERR_FILE" 2>/dev/null; then
            local tbl
            tbl="$(grep -o 'permission denied for table [A-Za-z0-9_.]*' "$ERR_FILE" | tail -1 | awk '{print $NF}')"
            echo "HINT: table $tbl is not owned by the jt-ipam database role, so pg_dump cannot read it." >&2
            echo "      If it is a leftover copy, either drop it or give it to the app role, e.g.:" >&2
            echo "      sudo -u postgres psql -d ${PG_DB:-jt_ipam} -c 'ALTER TABLE public.$tbl OWNER TO ${PG_USER:-jt_ipam}'" >&2
        fi
        write_status fail "$code"
        # A failed run must not leave an empty dated directory behind: it looks like a backup
        if [[ -n "$TARGET_DIR" && -d "$TARGET_DIR" ]]; then
            rm -f "$TARGET_DIR"/*.partial
            rmdir "$TARGET_DIR" 2>/dev/null || true
        fi
        echo "[$(date -Iseconds)] backup FAILED (exit $code)" >&2
    fi
    [[ -n "$ERR_FILE" ]] && rm -f "$ERR_FILE"
}
trap on_exit EXIT

ERR_FILE="$(mktemp)"

if [[ ! -r "$ENV_FILE" ]]; then
    echo "FATAL: cannot read $ENV_FILE" | tee -a "$ERR_FILE" >&2
    exit 1
fi

# shellcheck disable=SC1090
set -a; source <(grep -E '^(POSTGRES_|PG)' "$ENV_FILE"); set +a

PG_DB="${POSTGRES_DB:-jt_ipam}"
PG_USER="${POSTGRES_USER:-jt_ipam}"
PG_HOST="${POSTGRES_HOST:-127.0.0.1}"
PG_PORT="${POSTGRES_PORT:-5432}"

DATE_STAMP="$(date +%F)"
TARGET_DIR="$BACKUP_DIR/$DATE_STAMP"

install -d -m 0700 -o jtipam -g jtipam "$BACKUP_DIR" 2>/dev/null || \
  install -d -m 0700 "$BACKUP_DIR"
install -d -m 0700 "$TARGET_DIR"

echo "[$(date -Iseconds)] backup starting → $TARGET_DIR"

# ── 1. pg_dump ──
# Write to a temporary name and rename on success: a failed run later the same day
# (for example the backup an upgrade takes) must not truncate the good dump from 03:30.
DUMP_FILE="$TARGET_DIR/jt-ipam-${DATE_STAMP}.dump"
# stderr goes to a file first (synchronously, so the exit trap can quote it), then to the journal
if ! PGPASSWORD="${POSTGRES_PASSWORD:-}" pg_dump \
    -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" \
    -Fc --no-owner --no-acl \
    -f "$DUMP_FILE.partial" "$PG_DB" 2>>"$ERR_FILE"; then
    cat "$ERR_FILE" >&2
    exit 1
fi
[[ -s "$ERR_FILE" ]] && cat "$ERR_FILE" >&2
mv -f "$DUMP_FILE.partial" "$DUMP_FILE"
chmod 0600 "$DUMP_FILE"
DUMP_SIZE="$(du -h "$DUMP_FILE" | awk '{print $1}')"
echo "  pg_dump: $DUMP_SIZE"

# ── 2. env ──
cp -p "$ENV_FILE" "$TARGET_DIR/backend.env"
chmod 0600 "$TARGET_DIR/backend.env"

# ── 3. TLS certs (if present) ──
if [[ -d "$TLS_DIR" ]]; then
    tar -czf "$TARGET_DIR/tls.tar.gz" -C "$(dirname "$TLS_DIR")" "$(basename "$TLS_DIR")" 2>/dev/null
    chmod 0600 "$TARGET_DIR/tls.tar.gz"
fi

# ── 3b. Uploaded files (data-center floor plans, etc.); stored on filesystem, must be backed up together for a full restore ──
UPLOAD_DIR="${UPLOAD_DIR:-/var/lib/jt-ipam/uploads}"
if [[ -d "$UPLOAD_DIR" ]]; then
    tar -czf "$TARGET_DIR/uploads.tar.gz" -C "$(dirname "$UPLOAD_DIR")" "$(basename "$UPLOAD_DIR")" 2>/dev/null
    chmod 0600 "$TARGET_DIR/uploads.tar.gz"
    echo "  uploads: $(du -h "$TARGET_DIR/uploads.tar.gz" 2>/dev/null | awk '{print $1}')"
fi

# ── 4. Prune expired backups ──
find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -mtime "+$RETENTION_DAYS" -exec rm -rf {} +

write_status ok 0
echo "[$(date -Iseconds)] backup OK"
