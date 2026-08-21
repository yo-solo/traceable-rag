#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

if [ "$(id -u)" -eq 0 ]; then
  printf '%s\n' "Run local deployment as a non-root developer account." >&2
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
fi

if [ ! -f .env.local ]; then
  cp .env.local.example .env.local
  chmod 600 .env.local
fi

read_local_value() {
  key=$1
  value=$(sed -n "s/^$key=//p" .env.local | sed -n '1p' | tr -d '\r')
  case "$value" in
    \"*\") value=${value#\"}; value=${value%\"} ;;
    \'*\') value=${value#\'}; value=${value%\'} ;;
  esac
  printf '%s' "$value"
}

configured_secret_dir=$(read_local_value TRACEABLE_RAG_DEV_SECRET_DIR)
configured_data_dir=$(read_local_value TRACEABLE_RAG_DATA_DIR)
SECRET_DIR=${TRACEABLE_RAG_DEV_SECRET_DIR:-${configured_secret_dir:-$ROOT_DIR/.runtime/dev-secrets}}
DATA_DIR=${TRACEABLE_RAG_DATA_DIR:-${configured_data_dir:-$ROOT_DIR/.runtime/dev-data-v3}}
case "$SECRET_DIR" in /*) ;; *) SECRET_DIR=$ROOT_DIR/$SECRET_DIR ;; esac
case "$DATA_DIR" in /*) ;; *) DATA_DIR=$ROOT_DIR/$DATA_DIR ;; esac
mkdir -p "$SECRET_DIR" "$DATA_DIR/home" "$DATA_DIR/managed" "$ROOT_DIR/.runtime"
chmod 700 "$SECRET_DIR"
chmod 700 "$DATA_DIR" "$DATA_DIR/home" "$DATA_DIR/managed"

if [ ! -e "$SECRET_DIR/master.key" ]; then
  dd if=/dev/urandom of="$SECRET_DIR/master.key.tmp" bs=32 count=1 2>/dev/null
  chmod 600 "$SECRET_DIR/master.key.tmp"
  mv "$SECRET_DIR/master.key.tmp" "$SECRET_DIR/master.key"
fi
[ -f "$SECRET_DIR/master.key" ] && [ ! -L "$SECRET_DIR/master.key" ] || {
  printf '%s\n' "Development master key must be a regular file." >&2
  exit 1
}
[ "$(wc -c < "$SECRET_DIR/master.key" | tr -d ' ')" = 32 ] || {
  printf '%s\n' "Development master key must contain exactly 32 raw bytes." >&2
  exit 1
}

if [ ! -e "$SECRET_DIR/admin-token" ]; then
  dd if=/dev/urandom bs=32 count=1 2>/dev/null | od -An -v -tx1 | tr -d ' \n' > "$SECRET_DIR/admin-token.tmp"
  chmod 600 "$SECRET_DIR/admin-token.tmp"
  mv "$SECRET_DIR/admin-token.tmp" "$SECRET_DIR/admin-token"
fi
[ -f "$SECRET_DIR/admin-token" ] && [ ! -L "$SECRET_DIR/admin-token" ] || {
  printf '%s\n' "Development admin token must be a regular file." >&2
  exit 1
}
grep -Eq '^[0-9a-f]{64}$' "$SECRET_DIR/admin-token" || {
  printf '%s\n' "Development admin token must encode 32 random bytes as hex." >&2
  exit 1
}

USER_OVERRIDE=$ROOT_DIR/.runtime/dev-compose-user.yaml
{
  printf 'services:\n'
  printf '  traceable-rag:\n'
  printf '    user: "%s:%s"\n' "$(id -u)" "$(id -g)"
} > "$USER_OVERRIDE.tmp"
chmod 600 "$USER_OVERRIDE.tmp"
mv "$USER_OVERRIDE.tmp" "$USER_OVERRIDE"

export TRACEABLE_RAG_DEV_SECRET_DIR=$SECRET_DIR
export TRACEABLE_RAG_DATA_DIR=$DATA_DIR

set -- docker compose \
  --env-file .env \
  --env-file .env.local \
  -f compose.yaml \
  -f compose.local.yaml \
  -f "$USER_OVERRIDE"

"$@" up --build --detach traceable-rag

attempt=0
until "$@" exec -T traceable-rag \
  curl --fail --silent --show-error http://127.0.0.1:8000/api/health \
  >/dev/null 2>&1
do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    "$@" ps
    printf '%s\n' "Traceable RAG did not become healthy within 60 seconds." >&2
    exit 1
  fi
  sleep 1
done

"$@" ps
printf '%s\n' "Traceable RAG local deployment is healthy."
