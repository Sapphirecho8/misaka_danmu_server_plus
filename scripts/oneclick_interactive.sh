#!/usr/bin/env bash
set -euo pipefail

# Interactive one-click installer for misaka_danmu_server (Debian/Ubuntu)
# - Asks for MySQL availability and config
# - Installs MySQL (apt or Docker) if needed and creates DB/user
# - Asks for JWT secret, server port, admin username/password, TMDB API key
# - Patches backend config (YAML) and code (tmdb.py), hides admin password change on UI
# - Builds frontend, sets up systemd, and starts service

APP_DIR="/root/misaka_danmu_server"
APP_PORT_DEFAULT="7768"
DB_NAME_DEFAULT="danmaku_db"
DB_USER_DEFAULT="danmaku_user"
DB_PASS_DEFAULT=""
DB_HOST_DEFAULT="127.0.0.1"
DB_PORT_DEFAULT="3306"
JWT_DEFAULT=""
ADMIN_USER_DEFAULT="admin"
ADMIN_PASS_DEFAULT=""

log(){ echo -e "[oneclick][$(date +'%F %T')] $*"; }
die(){ echo "[oneclick][ERROR] $*" >&2; exit 1; }

require_root(){ if [ "${EUID:-$(id -u)}" -ne 0 ]; then die "Please run as root."; fi; }

ask(){
  # ask "Prompt" default_var_name default_value -> sets REPLY
  local prompt="$1"; shift
  local def="$1"; shift || true
  if [ -n "$def" ]; then
    read -r -p "$prompt [$def]: " REPLY || true
    REPLY="${REPLY:-$def}"
  else
    read -r -p "$prompt: " REPLY || true
  fi
}

ask_secret(){
  local prompt="$1"; shift
  local def="$1"; shift || true
  read -r -s -p "$prompt${def:+ [$def]}: " REPLY || true
  echo
  REPLY="${REPLY:-$def}"
}

ensure_base_packages(){
  log "Installing base packages..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y git curl ca-certificates gnupg lsb-release build-essential \
    python3 python3-venv python3-pip python3-yaml openssl jq
}

ensure_node(){
  if ! command -v node >/dev/null 2>&1; then
    log "Installing Node.js 20..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs
  fi
}

ensure_docker(){
  if ! command -v docker >/dev/null 2>&1; then
    log "Installing Docker..."
    curl -fsSL https://get.docker.com | bash
    systemctl enable --now docker
  fi
}

create_mysql_db_and_user(){
  local host="$1" port="$2" dbname="$3" dbuser="$4" dbpass="$5"
  log "Creating database '$dbname' and user '$dbuser' if not exist..."
  # Try local root socket first (Debian/MariaDB default)
  if command -v mysql >/dev/null 2>&1; then
    if mysql --protocol=socket -uroot -e "SELECT 1" >/dev/null 2>&1; then
      mysql --protocol=socket -uroot <<SQL
CREATE DATABASE IF NOT EXISTS \`$dbname\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '$dbuser'@'%' IDENTIFIED BY '$dbpass';
GRANT ALL PRIVILEGES ON \`$dbname\`.* TO '$dbuser'@'%';
FLUSH PRIVILEGES;
SQL
      return 0
    fi
  fi

  # Fallback: need root password to connect over TCP if not socket
  echo "Root socket auth not available. If you installed Docker MySQL, this step may already be done." \
    "Otherwise, please ensure database and user exist: $dbname / $dbuser" >&2
}

patch_yaml_config(){
  local yaml="/app/config/config.yml"
  local host="$1" port="$2" dbhost="$3" dbport="$4" dbuser="$5" dbpass="$6" dbname="$7" jwt_secret="$8" admin_user="$9" admin_pass="${10}" rate_limit_per_hour="${11}"
  python3 - "$yaml" "$host" "$port" "$dbhost" "$dbport" "$dbuser" "$dbpass" "$dbname" "$jwt_secret" "$admin_user" "$admin_pass" "$rate_limit_per_hour" <<'PY'
import sys, yaml
from pathlib import Path

yaml_path = Path(sys.argv[1])
cfg = {}
if yaml_path.exists():
    with open(yaml_path, 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f) or {}

def ensure(d, key, default):
    if key not in d or not isinstance(d[key], dict):
        d[key] = {}
    return d[key]

server_host, server_port, db_host, db_port, db_user, db_pass, db_name, jwt_secret, admin_user, admin_pass, rate_limit_per_hour = sys.argv[2:13]

server = ensure(cfg, 'server', {})
server['host'] = server_host
server['port'] = int(server_port)

db = ensure(cfg, 'database', {})
db['host'] = db_host
db['port'] = int(db_port)
db['user'] = db_user
db['password'] = db_pass
db['name'] = db_name

jwt = ensure(cfg, 'jwt', {})
jwt['secret_key'] = jwt_secret
jwt['algorithm'] = 'HS256'
jwt['access_token_expire_minutes'] = 1440

cfg['environment'] = 'production'

# Add admin section so backend uses provided values
cfg['admin'] = {'initial_user': admin_user, 'initial_password': admin_pass}

# Add rate limit section
try:
    per_hour = int(rate_limit_per_hour)
except Exception:
    per_hour = 1000
cfg['rateLimit'] = { 'per_hour': per_hour }

yaml_path.parent.mkdir(parents=True, exist_ok=True)
with open(yaml_path, 'w', encoding='utf-8') as f:
    yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
print('config.yml updated at', yaml_path)
PY
  
}

patch_tmdb_api_key(){
  local key="$1"
  local file="$APP_DIR/src/metadata_sources/tmdb.py"
  if [ ! -f "$file" ]; then die "tmdb.py not found at $file"; fi
  sed -i -E "s#api_key = \"[A-Za-z0-9]+\"#api_key = \"$key\"#" "$file"
}

ensure_app_layout(){
  log "Preparing /app layout..."
  mkdir -p /app/config /app/config/image /app/static/swagger-ui
  cp -r "$APP_DIR/static/swagger-ui/"* /app/static/swagger-ui/ 2>/dev/null || true
  [ -L /app/src ] || ln -sfn "$APP_DIR/src" /app/src
  [ -f /app/config/config.yml ] || cp "$APP_DIR/config/config.yml" /app/config/config.yml
}

build_backend_frontend(){
  log "Creating Python venv + installing deps..."
  python3 -m venv "$APP_DIR/.venv"
  # shellcheck disable=SC1091
  source "$APP_DIR/.venv/bin/activate"
  python3 -m pip install --upgrade pip
  pip install -r "$APP_DIR/requirements.txt"

  log "Building frontend..."
  pushd "$APP_DIR/web" >/dev/null
  npm ci
  # Pass admin username to frontend at build-time to hide password UI for admin
  VITE_ADMIN_USERNAME="${ADMIN_USER}" npm run build
  popd >/dev/null
}

install_systemd(){
  local port="$1"
  log "Installing systemd service (port $port)..."
  cat >/etc/systemd/system/misaka-danmu.service <<SERVICE
[Unit]
Description=Misaka Danmaku Server (Uvicorn)
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=/bin/bash -lc 'source $APP_DIR/.venv/bin/activate && python3 -m uvicorn src.main:app --host 0.0.0.0 --port $port'
Restart=on-failure
RestartSec=5
User=root
Environment=PYTHONUNBUFFERED=1
Environment=TZ=Asia/Shanghai

[Install]
WantedBy=multi-user.target
SERVICE
  systemctl daemon-reload
  systemctl enable --now misaka-danmu
}

print_summary(){
  echo
  echo "==== Deployment Complete ===="
  echo "URL: http://<your-server-ip>:${APP_PORT}"
  echo "Admin: ${ADMIN_USER}"
  echo "JWT secret set; TMDB API key applied (hidden in UI)."
}

############################ MAIN ############################
require_root
ensure_base_packages

if [ ! -d "$APP_DIR" ]; then
  die "Project directory not found: $APP_DIR"
fi

log "Checking for MySQL client..."
HAS_MYSQL=0
if command -v mysql >/dev/null 2>&1; then HAS_MYSQL=1; fi

if ss -ltn 2>/dev/null | grep -q ":${DB_PORT_DEFAULT} "; then DB_PORT_INUSE=1; else DB_PORT_INUSE=0; fi

echo
echo "MySQL detected: $HAS_MYSQL (client), port ${DB_PORT_DEFAULT} listening: $DB_PORT_INUSE"
ask "Use existing MySQL? (Y/n)" "Y"
USE_EXISTING_MYSQL="$REPLY"

# Ask DB connection params (needed also for Docker env)
ask "Database host" "$DB_HOST_DEFAULT"; DB_HOST="$REPLY"
ask "Database port" "$DB_PORT_DEFAULT"; DB_PORT="$REPLY"
ask "Database name" "$DB_NAME_DEFAULT"; DB_NAME="$REPLY"
ask "Database user" "$DB_USER_DEFAULT"; DB_USER="$REPLY"
ask_secret "Database password" "$DB_PASS_DEFAULT"; DB_PASS="$REPLY"

if [[ "$USE_EXISTING_MYSQL" =~ ^([Nn]|no)$ ]]; then
  echo "Choose MySQL install method:"
  echo "  1) apt (MariaDB server)"
  echo "  2) Docker (mysql:8.1.0-oracle)"
  ask "Install method (1/2)" "1"
  INSTALL_METHOD="$REPLY"

  if [ "$INSTALL_METHOD" = "1" ]; then
    log "Installing MariaDB server via apt..."
    apt-get install -y mariadb-server
    systemctl enable --now mariadb
  else
    ensure_docker
    ask_secret "Enter MySQL root password for Docker" "misaka_root_pwd"
    MYSQL_ROOT_PWD="$REPLY"
    log "Starting Docker MySQL (with DB and user)..."
    docker rm -f danmu-mysql >/dev/null 2>&1 || true
    docker run -d --name danmu-mysql \
      -e MYSQL_ROOT_PASSWORD="$MYSQL_ROOT_PWD" \
      -e MYSQL_DATABASE="$DB_NAME" \
      -e MYSQL_USER="$DB_USER" \
      -e MYSQL_PASSWORD="$DB_PASS" \
      -p ${DB_PORT}:3306 \
      mysql:8.1.0-oracle
    log "Waiting 25s for MySQL Docker to initialize..."
    sleep 25
  fi
fi

# Create DB/user if possible (local installs)
create_mysql_db_and_user "$DB_HOST" "$DB_PORT" "$DB_NAME" "$DB_USER" "$DB_PASS" || true

# Server/app config
ask "HTTP port for the UI/API" "$APP_PORT_DEFAULT"; APP_PORT="$REPLY"
if [ -z "$APP_PORT" ]; then APP_PORT="$APP_PORT_DEFAULT"; fi

RAND_JWT=$(openssl rand -hex 32 | tr -d '\n')
ask_secret "JWT secret key" "$RAND_JWT"; JWT_SECRET="$REPLY"

ask "Admin username" "$ADMIN_USER_DEFAULT"; ADMIN_USER="$REPLY"
ask_secret "Admin password" "$ADMIN_PASS_DEFAULT"; ADMIN_PASS="$REPLY"
[ -n "$ADMIN_PASS" ] || die "Admin password cannot be empty."

ask "TMDB API Key (v3)" ""; TMDB_KEY="$REPLY"
[ -n "$TMDB_KEY" ] || die "TMDB API Key cannot be empty."

# New: ask per-hour rate limit
ask "Global rate limit per hour" "1000"; RATE_LIMIT_PER_HOUR="$REPLY"
if ! [[ "$RATE_LIMIT_PER_HOUR" =~ ^[0-9]+$ ]]; then
  die "Rate limit must be an integer"
fi

ensure_app_layout
patch_yaml_config "0.0.0.0" "$APP_PORT" "$DB_HOST" "$DB_PORT" "$DB_USER" "$DB_PASS" "$DB_NAME" "$JWT_SECRET" "$ADMIN_USER" "$ADMIN_PASS" "$RATE_LIMIT_PER_HOUR"
patch_tmdb_api_key "$TMDB_KEY"

ensure_node
build_backend_frontend
install_systemd "$APP_PORT"

print_summary
