#!/bin/sh
set -eu

umask 077

DEFAULT_VERSION='@TRACEABLE_RAG_VERSION@'
UNRENDERED_VERSION=$(printf '@%s@' TRACEABLE_RAG_VERSION)
REPOSITORY=${TRACEABLE_RAG_REPOSITORY:-yo-solo/traceable-rag}
IMAGE_REPOSITORY=${TRACEABLE_RAG_IMAGE_REPOSITORY:-ghcr.io/yo-solo/traceable-rag-public}

die() {
  printf 'Traceable RAG installer: %s\n' "$*" >&2
  exit 1
}

script_dir() {
  case "$0" in
    */install.sh|install.sh) ;;
    *) return 1 ;;
  esac
  [ -f "$0" ] || return 1
  CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd
}

sha256_file() {
  file=$1
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" | awk '{print $1}'
  else
    die "sha256sum or shasum is required to verify release files"
  fi
}

manifest_string() {
  file=$1
  key=$2
  sed -n "s/^[[:space:]]*\"$key\":[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$file" | sed -n '1p'
}

validate_release_manifest() {
  manifest_file=$1
  expected_version=$2
  [ -f "$manifest_file" ] && [ ! -L "$manifest_file" ] || die "release manifest is missing or unsafe: $manifest_file"
  manifest_version=$(manifest_string "$manifest_file" version)
  manifest_schema=$(sed -n 's/^[[:space:]]*"schema_version":[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$manifest_file" | sed -n '1p')
  manifest_image=$(manifest_string "$manifest_file" image)
  manifest_digest=$(manifest_string "$manifest_file" index_digest)
  manifest_source_commit=$(manifest_string "$manifest_file" source_commit)
  manifest_amd64=$(sed -n 's/^[[:space:]]*"linux\/amd64":[[:space:]]*"\(sha256:[0-9a-f]*\)".*/\1/p' "$manifest_file" | sed -n '1p')
  manifest_arm64=$(sed -n 's/^[[:space:]]*"linux\/arm64":[[:space:]]*"\(sha256:[0-9a-f]*\)".*/\1/p' "$manifest_file" | sed -n '1p')
  [ "$manifest_schema" = 1 ] || die "unsupported release manifest schema version"
  [ "$manifest_version" = "$expected_version" ] || die "release manifest version mismatch"
  [ "$manifest_image" = "$IMAGE_REPOSITORY" ] || die "release manifest image repository mismatch"
  printf '%s\n' "$manifest_digest" | grep -Eq '^sha256:[0-9a-f]{64}$' || die "release manifest index digest is invalid"
  printf '%s\n' "$manifest_amd64" | grep -Eq '^sha256:[0-9a-f]{64}$' || die "release manifest linux/amd64 digest is invalid"
  printf '%s\n' "$manifest_arm64" | grep -Eq '^sha256:[0-9a-f]{64}$' || die "release manifest linux/arm64 digest is invalid"
  printf '%s\n' "$manifest_source_commit" | grep -Eq '^[0-9a-f]{40}$' || die "release manifest source commit is invalid"
  MANIFEST_IMAGE=$manifest_image
  MANIFEST_DIGEST=$manifest_digest
  MANIFEST_SOURCE_COMMIT=$manifest_source_commit
  export MANIFEST_IMAGE MANIFEST_DIGEST MANIFEST_SOURCE_COMMIT
}

SCRIPT_DIR=$(script_dir || printf '%s' '')

resolve_version() {
  requested=${TRACEABLE_RAG_VERSION:-$DEFAULT_VERSION}
  if [ "$requested" = "$UNRENDERED_VERSION" ]; then
    if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/pyproject.toml" ]; then
      requested=$(sed -n 's/^version = "\([^"]*\)"/v\1/p' "$SCRIPT_DIR/pyproject.toml" | sed -n '1p')
    else
      die "use the versioned install.sh attached to a GitHub Release"
    fi
  fi
  case "$requested" in
    v*) ;;
    *) requested=v$requested ;;
  esac
  printf '%s\n' "$requested" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$' || die "TRACEABLE_RAG_VERSION must be an exact release such as v0.4.2"
  printf '%s' "$requested"
}

VERSION=$(resolve_version)

HOME_DIR=${HOME:?HOME is required}
EXPLICIT_DATA_DIR=${TRACEABLE_RAG_DATA_DIR+x}
EXPLICIT_STATE_DIR=${TRACEABLE_RAG_STATE_DIR+x}
EXPLICIT_BACKUP_DIR=${TRACEABLE_RAG_BACKUP_DIR+x}
EXPLICIT_BIN_DIR=${TRACEABLE_RAG_BIN_DIR+x}
EXPLICIT_PROJECT_NAME=${TRACEABLE_RAG_PROJECT_NAME+x}
EXPLICIT_BIND_ADDRESS=${TRACEABLE_RAG_BIND_ADDRESS+x}
EXPLICIT_PORT=${TRACEABLE_RAG_PORT+x}
EXPLICIT_READY_TIMEOUT=${TRACEABLE_RAG_READY_TIMEOUT_SECONDS+x}
EXPLICIT_IMAGE_REPOSITORY=${TRACEABLE_RAG_IMAGE_REPOSITORY+x}
EXPLICIT_RELEASE_REPOSITORY=${TRACEABLE_RAG_REPOSITORY+x}
CONFIG_DIR=${TRACEABLE_RAG_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME_DIR/.config}/traceable-rag}
DATA_DIR=${TRACEABLE_RAG_DATA_DIR:-${XDG_DATA_HOME:-$HOME_DIR/.local/share}/traceable-rag}
STATE_DIR=${TRACEABLE_RAG_STATE_DIR:-${XDG_STATE_HOME:-$HOME_DIR/.local/state}/traceable-rag}
BACKUP_DIR=${TRACEABLE_RAG_BACKUP_DIR:-$STATE_DIR/backups}
BIN_DIR=${TRACEABLE_RAG_BIN_DIR:-${XDG_BIN_HOME:-$HOME_DIR/.local/bin}}
BIN_LINK=$BIN_DIR/traceable-rag
MASTER_KEY_FILE=$CONFIG_DIR/secrets/master.key
ADMIN_TOKEN_FILE=$CONFIG_DIR/secrets/admin-token
ENV_FILE=$CONFIG_DIR/deployment.env
PROJECT_NAME=${TRACEABLE_RAG_PROJECT_NAME:-traceable-rag-user}
BIND_ADDRESS=${TRACEABLE_RAG_BIND_ADDRESS:-127.0.0.1}
if [ "$BIND_ADDRESS" = localhost ]; then BIND_ADDRESS=127.0.0.1; fi
PORT=${TRACEABLE_RAG_PORT:-8000}
READY_TIMEOUT=${TRACEABLE_RAG_READY_TIMEOUT_SECONDS:-900}
IMAGE=$IMAGE_REPOSITORY:$VERSION
RELEASE_SOURCE_COMMIT=
STRICT_RELEASE_IDENTITY=0
PORT_CHANGE_REQUESTED=0

stored_value() {
  sed -n "s/^$1=//p" "$ENV_FILE" | sed -n '1p'
}

if [ -f "$ENV_FILE" ]; then
  existing_release=$(stored_value TRACEABLE_RAG_RELEASE_VERSION)
  [ -n "$existing_release" ] || die "existing deployment is missing TRACEABLE_RAG_RELEASE_VERSION"
  [ "$existing_release" = "$VERSION" ] || die "installed release is $existing_release; use traceable-rag upgrade $VERSION instead of rerunning the installer"

  stored_data=$(stored_value TRACEABLE_RAG_DATA_DIR)
  stored_state=$(stored_value TRACEABLE_RAG_STATE_DIR)
  stored_backup=$(stored_value TRACEABLE_RAG_BACKUP_DIR)
  stored_bin_link=$(stored_value TRACEABLE_RAG_BIN_LINK)
  stored_project=$(stored_value TRACEABLE_RAG_PROJECT_NAME)
  stored_bind=$(stored_value TRACEABLE_RAG_BIND_ADDRESS)
  stored_port=$(stored_value TRACEABLE_RAG_PORT)
  stored_timeout=$(stored_value TRACEABLE_RAG_READY_TIMEOUT_SECONDS)
  stored_repository=$(stored_value TRACEABLE_RAG_IMAGE_REPOSITORY)
  stored_release_repository=$(stored_value TRACEABLE_RAG_RELEASE_REPOSITORY)
  stored_image=$(stored_value TRACEABLE_RAG_IMAGE)
  stored_source_commit=$(stored_value TRACEABLE_RAG_RELEASE_SOURCE_COMMIT)
  stored_master=$(stored_value TRACEABLE_RAG_MASTER_KEY_HOST_FILE)
  stored_admin=$(stored_value TRACEABLE_RAG_ADMIN_TOKEN_HOST_FILE)
  stored_uid=$(stored_value TRACEABLE_RAG_CONTAINER_UID)
  stored_gid=$(stored_value TRACEABLE_RAG_CONTAINER_GID)

  [ -n "$stored_port" ] || die "existing deployment is missing TRACEABLE_RAG_PORT"

  [ "$EXPLICIT_DATA_DIR" != x ] || [ "$TRACEABLE_RAG_DATA_DIR" = "$stored_data" ] || die "TRACEABLE_RAG_DATA_DIR differs from the installed layout"
  [ "$EXPLICIT_STATE_DIR" != x ] || [ "$TRACEABLE_RAG_STATE_DIR" = "$stored_state" ] || die "TRACEABLE_RAG_STATE_DIR differs from the installed layout"
  [ "$EXPLICIT_BACKUP_DIR" != x ] || [ "$TRACEABLE_RAG_BACKUP_DIR" = "$stored_backup" ] || die "TRACEABLE_RAG_BACKUP_DIR differs from the installed layout"
  [ "$EXPLICIT_BIN_DIR" != x ] || [ "$TRACEABLE_RAG_BIN_DIR/traceable-rag" = "$stored_bin_link" ] || die "TRACEABLE_RAG_BIN_DIR differs from the installed layout"
  [ "$EXPLICIT_PROJECT_NAME" != x ] || [ "$TRACEABLE_RAG_PROJECT_NAME" = "$stored_project" ] || die "TRACEABLE_RAG_PROJECT_NAME differs from the installed layout"
  requested_bind=${TRACEABLE_RAG_BIND_ADDRESS:-}
  [ "$requested_bind" != localhost ] || requested_bind=127.0.0.1
  [ "$EXPLICIT_BIND_ADDRESS" != x ] || [ "$requested_bind" = "$stored_bind" ] || die "TRACEABLE_RAG_BIND_ADDRESS differs from the installed layout"
  [ "$EXPLICIT_READY_TIMEOUT" != x ] || [ "$TRACEABLE_RAG_READY_TIMEOUT_SECONDS" = "$stored_timeout" ] || die "TRACEABLE_RAG_READY_TIMEOUT_SECONDS differs from the installed layout"
  [ "$EXPLICIT_IMAGE_REPOSITORY" != x ] || [ "$TRACEABLE_RAG_IMAGE_REPOSITORY" = "$stored_repository" ] || die "TRACEABLE_RAG_IMAGE_REPOSITORY differs from the installed layout"
  [ "$EXPLICIT_RELEASE_REPOSITORY" != x ] || [ "$TRACEABLE_RAG_REPOSITORY" = "$stored_release_repository" ] || die "TRACEABLE_RAG_REPOSITORY differs from the installed layout"

  DATA_DIR=$stored_data
  STATE_DIR=$stored_state
  BACKUP_DIR=$stored_backup
  BIN_LINK=$stored_bin_link
  BIN_DIR=$(dirname "$stored_bin_link")
  PROJECT_NAME=$stored_project
  BIND_ADDRESS=$stored_bind
  if [ "$EXPLICIT_PORT" = x ]; then
    PORT=$TRACEABLE_RAG_PORT
    if [ "$PORT" != "$stored_port" ]; then PORT_CHANGE_REQUESTED=1; fi
  else
    PORT=$stored_port
  fi
  READY_TIMEOUT=$stored_timeout
  IMAGE_REPOSITORY=$stored_repository
  REPOSITORY=$stored_release_repository
  IMAGE=$stored_image
  RELEASE_SOURCE_COMMIT=$stored_source_commit
  if [ -n "$stored_source_commit" ]; then STRICT_RELEASE_IDENTITY=1; fi
  MASTER_KEY_FILE=$stored_master
  ADMIN_TOKEN_FILE=$stored_admin
  [ "$stored_uid" = "$(id -u)" ] && [ "$stored_gid" = "$(id -g)" ] || die "rerun the installer as the original deployment owner"
fi

[ "$CONFIG_DIR" != "$DATA_DIR" ] && [ "$CONFIG_DIR" != "$STATE_DIR" ] && [ "$DATA_DIR" != "$STATE_DIR" ] || die "config, data and state directories must be distinct"
for directory in "$CONFIG_DIR" "$DATA_DIR" "$STATE_DIR" "$BACKUP_DIR" "$BIN_DIR"; do
  case "$directory" in /*) ;; *) die "installation directories must be absolute: $directory" ;; esac
done
case "$CONFIG_DIR/" in "$DATA_DIR/"*|"$STATE_DIR/"*) die "config directory must not be nested inside data or state" ;; esac
case "$DATA_DIR/" in "$CONFIG_DIR/"*|"$STATE_DIR/"*|"$BACKUP_DIR/"*) die "data directory must remain independent from config, state and backups" ;; esac
case "$STATE_DIR/" in "$CONFIG_DIR/"*|"$DATA_DIR/"*) die "state directory must remain independent from config and data" ;; esac
case "$BACKUP_DIR/" in "$CONFIG_DIR/"*|"$DATA_DIR/"*) die "backup directory must not be inside config or data" ;; esac

for value in "$CONFIG_DIR" "$DATA_DIR" "$STATE_DIR" "$BACKUP_DIR" "$BIN_DIR" "$PROJECT_NAME" "$BIND_ADDRESS" "$PORT" "$READY_TIMEOUT"; do
  if printf '%s' "$value" | LC_ALL=C grep -q '[[:cntrl:]]'; then
    die "installation values cannot contain control characters"
  fi
  case "$value" in
    *\$*|*#*) die "installation values cannot contain dollar signs or # because Compose env parsing would be ambiguous" ;;
  esac
done

case "$PORT" in
  ''|*[!0-9]*) die "TRACEABLE_RAG_PORT must be an integer" ;;
esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "TRACEABLE_RAG_PORT must be between 1 and 65535"
case "$READY_TIMEOUT" in ''|*[!0-9]*) die "TRACEABLE_RAG_READY_TIMEOUT_SECONDS must be an integer" ;; esac
[ "$READY_TIMEOUT" -ge 30 ] && [ "$READY_TIMEOUT" -le 86400 ] || die "TRACEABLE_RAG_READY_TIMEOUT_SECONDS must be between 30 and 86400"
printf '%s\n' "$PROJECT_NAME" | grep -Eq '^[a-z0-9][a-z0-9_-]*$' || die "TRACEABLE_RAG_PROJECT_NAME is invalid"
case "$BIND_ADDRESS" in
  127.0.0.1|localhost|::1) ;;
  *) die "the bundled deployment supports loopback binding only" ;;
esac

current_uid=$(id -u)
current_gid=$(id -g)
if [ "$current_uid" -eq 0 ] && [ ! -f "$ENV_FILE" ]; then
  die "run the installer as the non-root account that will operate Traceable RAG"
fi

temporary=
cleanup() {
  if [ -n "$temporary" ] && [ -d "$temporary" ]; then
    rm -rf -- "$temporary"
  fi
}
trap cleanup EXIT HUP INT TERM

bundle_dir=
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/deploy/compose.yaml" ] && [ -f "$SCRIPT_DIR/deploy/traceable-rag" ]; then
  bundle_dir=$SCRIPT_DIR/deploy
  if [ -f "$bundle_dir/release-manifest.json" ]; then
    validate_release_manifest "$bundle_dir/release-manifest.json" "$VERSION"
    IMAGE=$MANIFEST_IMAGE@$MANIFEST_DIGEST
    RELEASE_SOURCE_COMMIT=$MANIFEST_SOURCE_COMMIT
    STRICT_RELEASE_IDENTITY=1
  elif [ "$STRICT_RELEASE_IDENTITY" = 1 ]; then
    die "installed release identity requires a bundle release-manifest.json"
  fi
else
  command -v curl >/dev/null 2>&1 || die "curl is required to download the deployment bundle"
  command -v tar >/dev/null 2>&1 || die "tar is required to unpack the deployment bundle"
  temporary=$(mktemp -d "${TMPDIR:-/tmp}/traceable-rag-install.XXXXXX")
  asset=traceable-rag-deployment-$VERSION.tar.gz
  release_base=${TRACEABLE_RAG_RELEASE_BASE_URL:-https://github.com/$REPOSITORY/releases/download/$VERSION}
  curl --fail --location --silent --show-error "$release_base/$asset" --output "$temporary/$asset"
  curl --fail --location --silent --show-error "$release_base/release-manifest.json" --output "$temporary/release-manifest.json"
  curl --fail --location --silent --show-error "$release_base/SHA256SUMS" --output "$temporary/SHA256SUMS"
  expected=$(awk -v asset="$asset" '$2 == asset || $2 == "*" asset { print $1; exit }' "$temporary/SHA256SUMS")
  [ -n "$expected" ] || die "release checksum does not list $asset"
  actual=$(sha256_file "$temporary/$asset")
  [ "$actual" = "$expected" ] || die "deployment bundle checksum mismatch"
  manifest_expected=$(awk '$2 == "release-manifest.json" || $2 == "*release-manifest.json" { print $1; exit }' "$temporary/SHA256SUMS")
  [ -n "$manifest_expected" ] || die "release checksum does not list release-manifest.json"
  manifest_actual=$(sha256_file "$temporary/release-manifest.json")
  [ "$manifest_actual" = "$manifest_expected" ] || die "release manifest checksum mismatch"
  validate_release_manifest "$temporary/release-manifest.json" "$VERSION"
  IMAGE=$MANIFEST_IMAGE@$MANIFEST_DIGEST
  RELEASE_SOURCE_COMMIT=$MANIFEST_SOURCE_COMMIT
  STRICT_RELEASE_IDENTITY=1
  mkdir -p "$temporary/unpacked"
  tar -xzf "$temporary/$asset" -C "$temporary/unpacked"
  bundle_dir=$temporary/unpacked/deploy
  [ -f "$bundle_dir/release-manifest.json" ] || die "deployment bundle is missing release-manifest.json"
  cmp -s "$temporary/release-manifest.json" "$bundle_dir/release-manifest.json" || die "deployment bundle release manifest does not match the verified release asset"
fi

[ -f "$bundle_dir/compose.yaml" ] || die "deployment bundle is missing compose.yaml"
[ -f "$bundle_dir/traceable-rag" ] || die "deployment bundle is missing the POSIX manager"

installation_id=
if [ -f "$ENV_FILE" ]; then
  installation_id=$(stored_value TRACEABLE_RAG_INSTALLATION_ID)
fi
if [ -z "$installation_id" ]; then
  for owned_dir in "$CONFIG_DIR" "$DATA_DIR" "$STATE_DIR" "$BACKUP_DIR"; do
    marker=$owned_dir/.traceable-rag-installation
    if [ -f "$marker" ] && [ ! -L "$marker" ]; then
      marker_id=$(sed -n '1p' "$marker")
      if [ -z "$installation_id" ]; then
        installation_id=$marker_id
      elif [ "$installation_id" != "$marker_id" ]; then
        die "partial installation markers disagree; inspect the target directories"
      fi
    fi
  done
fi
if [ -z "$installation_id" ]; then
  installation_id=$(dd if=/dev/urandom bs=16 count=1 2>/dev/null | od -An -v -tx1 | tr -d ' \n')
fi
printf '%s\n' "$installation_id" | grep -Eq '^[0-9a-f]{32}$' || die "installation ID is malformed"

preflight_owned_dir() {
  owned_dir=$1
  marker=$owned_dir/.traceable-rag-installation
  if [ -e "$owned_dir" ]; then
    [ -d "$owned_dir" ] && [ ! -L "$owned_dir" ] || die "installation path must be a real directory: $owned_dir"
    if [ ! -e "$marker" ] && [ -n "$(find "$owned_dir" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
      die "refusing to claim a non-empty unmarked directory: $owned_dir"
    fi
  fi
  if [ -e "$marker" ]; then
    [ -f "$marker" ] && [ ! -L "$marker" ] || die "invalid installation marker: $marker"
    [ "$(sed -n '1p' "$marker")" = "$installation_id" ] || die "installation marker mismatch: $owned_dir"
  fi
}

for owned_dir in "$CONFIG_DIR" "$DATA_DIR" "$STATE_DIR" "$BACKUP_DIR"; do
  preflight_owned_dir "$owned_dir"
done

preflight_bin_entry() {
  if [ -L "$BIN_LINK" ]; then
    [ "$(readlink "$BIN_LINK")" = "$CONFIG_DIR/traceable-rag" ] || die "refusing to replace an unmanaged command entry: $BIN_LINK; choose another TRACEABLE_RAG_BIN_DIR"
    marker=$CONFIG_DIR/.traceable-rag-installation
    [ -f "$marker" ] && [ ! -L "$marker" ] && [ "$(sed -n '1p' "$marker")" = "$installation_id" ] || die "command entry is not owned by this installation: $BIN_LINK; choose another TRACEABLE_RAG_BIN_DIR"
  elif [ -e "$BIN_LINK" ]; then
    die "refusing to replace an existing command entry: $BIN_LINK; choose another TRACEABLE_RAG_BIN_DIR"
  fi
}

preflight_bin_entry

for owned_dir in "$CONFIG_DIR" "$DATA_DIR" "$STATE_DIR" "$BACKUP_DIR"; do
  mkdir -p "$owned_dir"
  marker=$owned_dir/.traceable-rag-installation
  if [ ! -e "$marker" ]; then
    printf '%s' "$installation_id" > "$marker"
    chmod 600 "$marker"
  fi
done
mkdir -p "$CONFIG_DIR/secrets" "$DATA_DIR/home" "$DATA_DIR/managed" "$STATE_DIR/upgrades" "$BIN_DIR"
chmod 700 "$CONFIG_DIR" "$CONFIG_DIR/secrets" "$DATA_DIR" "$DATA_DIR/home" "$DATA_DIR/managed" "$STATE_DIR" "$STATE_DIR/upgrades" "$BACKUP_DIR"

if [ ! -e "$MASTER_KEY_FILE" ]; then
  key_tmp=$MASTER_KEY_FILE.tmp.$$
  dd if=/dev/urandom of="$key_tmp" bs=32 count=1 2>/dev/null
  [ "$(wc -c < "$key_tmp" | tr -d ' ')" = 32 ] || die "could not generate a 32-byte master key"
  chmod 600 "$key_tmp"
  mv "$key_tmp" "$MASTER_KEY_FILE"
fi
[ -f "$MASTER_KEY_FILE" ] && [ ! -L "$MASTER_KEY_FILE" ] || die "master key must be a regular file"
[ "$(wc -c < "$MASTER_KEY_FILE" | tr -d ' ')" = 32 ] || die "existing master key is not exactly 32 raw bytes; it was not overwritten"

if [ ! -e "$ADMIN_TOKEN_FILE" ]; then
  token_tmp=$ADMIN_TOKEN_FILE.tmp.$$
  dd if=/dev/urandom bs=32 count=1 2>/dev/null | od -An -v -tx1 | tr -d ' \n' > "$token_tmp"
  printf '\n' >> "$token_tmp"
  # Store exactly 64 hex characters without a trailing newline.
  token_value=$(sed -n '1p' "$token_tmp")
  printf '%s' "$token_value" > "$token_tmp"
  printf '%s\n' "$token_value" | grep -Eq '^[0-9a-f]{64}$' || die "could not generate the admin token"
  chmod 600 "$token_tmp"
  mv "$token_tmp" "$ADMIN_TOKEN_FILE"
fi
[ -f "$ADMIN_TOKEN_FILE" ] && [ ! -L "$ADMIN_TOKEN_FILE" ] || die "admin token must be a regular file"
[ "$(wc -c < "$ADMIN_TOKEN_FILE" | tr -d ' ')" = 64 ] || die "existing admin token must contain 64 hex characters; it was not overwritten"
grep -Eq '^[0-9a-f]{64}$' "$ADMIN_TOKEN_FILE" || die "existing admin token is malformed"
chmod 600 "$MASTER_KEY_FILE" "$ADMIN_TOKEN_FILE"

ensure_setting() {
  key=$1
  value=$2
  if ! grep -q "^$key=" "$ENV_FILE" 2>/dev/null; then
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

update_port_setting_atomic() {
  new_port=$1
  port_tmp=$CONFIG_DIR/deployment.env.port.$$
  if ! awk -v port="$new_port" '
    BEGIN { count = 0 }
    /^TRACEABLE_RAG_PORT=/ { count += 1; print "TRACEABLE_RAG_PORT=" port; next }
    { print }
    END { if (count != 1) exit 42 }
  ' "$ENV_FILE" > "$port_tmp"; then
    rm -f -- "$port_tmp"
    die "existing deployment must contain exactly one TRACEABLE_RAG_PORT setting"
  fi
  chmod 600 "$port_tmp"
  mv "$port_tmp" "$ENV_FILE"
}

if [ ! -f "$ENV_FILE" ]; then
  {
    printf 'TRACEABLE_RAG_IMAGE_REPOSITORY=%s\n' "$IMAGE_REPOSITORY"
    printf 'TRACEABLE_RAG_RELEASE_REPOSITORY=%s\n' "$REPOSITORY"
    printf 'TRACEABLE_RAG_RELEASE_VERSION=%s\n' "$VERSION"
    if [ "$STRICT_RELEASE_IDENTITY" = 1 ]; then
      printf 'TRACEABLE_RAG_RELEASE_SOURCE_COMMIT=%s\n' "$RELEASE_SOURCE_COMMIT"
    fi
    printf 'TRACEABLE_RAG_IMAGE=%s\n' "$IMAGE"
    printf 'TRACEABLE_RAG_PROJECT_NAME=%s\n' "$PROJECT_NAME"
    printf 'TRACEABLE_RAG_BIND_ADDRESS=%s\n' "$BIND_ADDRESS"
    printf 'TRACEABLE_RAG_PORT=%s\n' "$PORT"
    printf 'TRACEABLE_RAG_READY_TIMEOUT_SECONDS=%s\n' "$READY_TIMEOUT"
    printf 'TRACEABLE_RAG_DATA_DIR=%s\n' "$DATA_DIR"
    printf 'TRACEABLE_RAG_MASTER_KEY_HOST_FILE=%s\n' "$MASTER_KEY_FILE"
    printf 'TRACEABLE_RAG_ADMIN_TOKEN_HOST_FILE=%s\n' "$ADMIN_TOKEN_FILE"
    printf 'TRACEABLE_RAG_STATE_DIR=%s\n' "$STATE_DIR"
    printf 'TRACEABLE_RAG_BACKUP_DIR=%s\n' "$BACKUP_DIR"
    printf 'TRACEABLE_RAG_BIN_LINK=%s\n' "$BIN_LINK"
    printf 'TRACEABLE_RAG_CONTAINER_UID=%s\n' "$current_uid"
    printf 'TRACEABLE_RAG_CONTAINER_GID=%s\n' "$current_gid"
    printf 'TRACEABLE_RAG_INSTALLATION_ID=%s\n' "$installation_id"
  } > "$ENV_FILE"
else
  ensure_setting TRACEABLE_RAG_RELEASE_REPOSITORY "$REPOSITORY"
  ensure_setting TRACEABLE_RAG_ADMIN_TOKEN_HOST_FILE "$ADMIN_TOKEN_FILE"
  ensure_setting TRACEABLE_RAG_READY_TIMEOUT_SECONDS "$READY_TIMEOUT"
  ensure_setting TRACEABLE_RAG_RELEASE_VERSION "$VERSION"
  if [ "$STRICT_RELEASE_IDENTITY" = 1 ]; then
    ensure_setting TRACEABLE_RAG_RELEASE_SOURCE_COMMIT "$RELEASE_SOURCE_COMMIT"
  fi
  ensure_setting TRACEABLE_RAG_CONTAINER_UID "$current_uid"
  ensure_setting TRACEABLE_RAG_CONTAINER_GID "$current_gid"
  ensure_setting TRACEABLE_RAG_BIN_LINK "$BIN_LINK"
  ensure_setting TRACEABLE_RAG_INSTALLATION_ID "$installation_id"
  if [ "$PORT_CHANGE_REQUESTED" = 1 ]; then
    update_port_setting_atomic "$PORT"
  fi
fi
chmod 600 "$ENV_FILE"

installed_uid=$(sed -n 's/^TRACEABLE_RAG_CONTAINER_UID=//p' "$ENV_FILE" | sed -n '1p')
installed_gid=$(sed -n 's/^TRACEABLE_RAG_CONTAINER_GID=//p' "$ENV_FILE" | sed -n '1p')
case "$installed_uid:$installed_gid" in
  *[!0-9:]*|:*|*:) die "existing deployment has an invalid container UID/GID" ;;
esac
[ "$installed_uid" -gt 0 ] || die "existing deployment requests forbidden container UID 0"

compose_tmp=$CONFIG_DIR/compose.yaml.tmp.$$
sed \
  -e "s/@TRACEABLE_RAG_CONTAINER_UID@/$installed_uid/g" \
  -e "s/@TRACEABLE_RAG_CONTAINER_GID@/$installed_gid/g" \
  "$bundle_dir/compose.yaml" > "$compose_tmp"
grep -Fq "user: \"$installed_uid:$installed_gid\"" "$compose_tmp" || die "could not render the non-root Compose user"
chmod 600 "$compose_tmp"
mv "$compose_tmp" "$CONFIG_DIR/compose.yaml"

if [ "$STRICT_RELEASE_IDENTITY" = 1 ]; then
  manifest_tmp=$CONFIG_DIR/release-manifest.json.tmp.$$
  cp "$bundle_dir/release-manifest.json" "$manifest_tmp"
  chmod 600 "$manifest_tmp"
  mv "$manifest_tmp" "$CONFIG_DIR/release-manifest.json"
fi

manager_tmp=$CONFIG_DIR/traceable-rag.tmp.$$
cp "$bundle_dir/traceable-rag" "$manager_tmp"
chmod 700 "$manager_tmp"
mv "$manager_tmp" "$CONFIG_DIR/traceable-rag"
ln -sfn "$CONFIG_DIR/traceable-rag" "$BIN_LINK"

printf 'Traceable RAG %s deployment files installed.\n' "$VERSION"
printf '  command: %s\n' "$BIN_LINK"
printf '  config:  %s\n' "$CONFIG_DIR"
printf '  data:    %s\n' "$DATA_DIR"
printf '  backups: %s\n' "$BACKUP_DIR"

case ":${PATH:-}:" in
  *:"$BIN_DIR":*) ;;
  *)
    escaped_bin=$(printf '%s' "$BIN_DIR" | sed "s/'/'\\\\''/g")
    printf '\nIMPORTANT: %s is not currently on PATH.\n' "$BIN_DIR"
    printf '  Current shell: export PATH='"'"'%s'"'"':"$PATH"\n' "$escaped_bin"
    printf '  Persistence: add that export line to ~/.zshrc (zsh) or ~/.bashrc (bash), then open a new terminal.\n'
    printf '  Until then, always use the absolute command: %s\n' "$BIN_LINK"
    ;;
esac

if [ "${TRACEABLE_RAG_NO_START:-0}" = 1 ]; then
  printf 'Automatic start skipped. Run: %s start\n' "$BIN_LINK"
else
  if ! "$CONFIG_DIR/traceable-rag" start; then
    if [ "$PORT" -lt 65535 ]; then retry_port=$((PORT + 1)); else retry_port=8000; fi
    printf '\nThe service did not start. If local port %s is occupied, rerun this same-version installer on another loopback port:\n' "$PORT" >&2
    if [ -n "$SCRIPT_DIR" ]; then
      escaped_installer=$(printf '%s' "$SCRIPT_DIR/install.sh" | sed "s/'/'\\\\''/g")
      printf "  TRACEABLE_RAG_PORT=%s sh '%s'\n" "$retry_port" "$escaped_installer" >&2
    else
      printf '  TRACEABLE_RAG_PORT=%s sh -c "$(curl -fsSL https://github.com/%s/releases/download/%s/install.sh)"\n' "$retry_port" "$REPOSITORY" "$VERSION" >&2
    fi
    printf 'The rerun keeps the existing data, master key, admin token, and installation ID.\n' >&2
    exit 1
  fi
  if ! "$CONFIG_DIR/traceable-rag" setup-url; then
    printf 'Run %s setup-url from an interactive terminal to reveal the admin configuration link.\n' "$BIN_LINK" >&2
  fi
fi
