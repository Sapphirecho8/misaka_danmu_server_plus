#!/usr/bin/env bash
set -euo pipefail

# One-click installer for modified misaka_danmu_server
# This script is intended to run ON the target Ubuntu host as root.

APP_DIR="/root/misaka_danmu_server"
APP_PORT="7768"
MYSQL_CONTAINER="danmu-mysql"
DB_NAME="danmaku_db"
DB_USER="danmaku_user"
DB_PASS="your_password_here"

log() { echo -e "[oneclick][$(date +'%F %T')] $*"; }

log "Updating apt index..."
apt-get update -y

log "Installing base packages..."
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  git curl ca-certificates gnupg lsb-release \
  build-essential python3 python3-venv python3-pip

if ! command -v node >/dev/null 2>&1; then
  log "Installing Node.js 20..."
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y nodejs
fi

if ! command -v docker >/dev/null 2>&1; then
  log "Installing Docker..."
  curl -fsSL https://get.docker.com | bash
  systemctl enable --now docker
fi

log "Ensuring MySQL container is running..."
if ! docker ps -a --format '{{.Names}}' | grep -q "^${MYSQL_CONTAINER}$"; then
  docker run -d --name "${MYSQL_CONTAINER}" \
    -e MYSQL_ROOT_PASSWORD=misaka_root_pwd \
    -e MYSQL_DATABASE="${DB_NAME}" \
    -e MYSQL_USER="${DB_USER}" \
    -e MYSQL_PASSWORD="${DB_PASS}" \
    -p 3306:3306 \
    mysql:8.1.0-oracle
else
  docker start "${MYSQL_CONTAINER}" || true
fi

log "Preparing /app layout and static assets..."
mkdir -p /app/config /app/config/image /app/static/swagger-ui
cp -r ${APP_DIR}/static/swagger-ui/* /app/static/swagger-ui/ 2>/dev/null || true
if [ ! -L /app/src ]; then
  ln -sfn ${APP_DIR}/src /app/src
fi
if [ ! -f /app/config/config.yml ]; then
  cp ${APP_DIR}/config/config.yml /app/config/config.yml
fi

log "Patching config.yml if needed (DB settings)..."
python3 - <<'PY'
from pathlib import Path
p = Path('/app/config/config.yml')
txt = p.read_text(encoding='utf-8')
txt = txt.replace('user: "danmaku_user"', 'user: "danmaku_user"')
txt = txt.replace('password: "your_password_here"', 'password: "your_password_here"')
txt = txt.replace('name: "danmaku_db"', 'name: "danmaku_db"')
p.write_text(txt, encoding='utf-8')
print('config.yml validated/updated')
PY

log "Creating Python virtualenv and installing backend deps..."
python3 -m venv ${APP_DIR}/.venv
source ${APP_DIR}/.venv/bin/activate
python3 -m pip install --upgrade pip
pip install -r ${APP_DIR}/requirements.txt

log "Building frontend..."
cd ${APP_DIR}/web
npm ci
npm run build

log "Creating systemd service..."
SERVICE_FILE="/etc/systemd/system/misaka-danmu.service"
cat > ${SERVICE_FILE} <<EOF
[Unit]
Description=Misaka Danmaku Server (Uvicorn)
After=network.target docker.service

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStart=/bin/bash -lc 'source ${APP_DIR}/.venv/bin/activate && python3 -m uvicorn src.main:app --host 0.0.0.0 --port ${APP_PORT}'
Restart=on-failure
RestartSec=5
User=root
Environment=PYTHONUNBUFFERED=1
Environment=TZ=Asia/Shanghai

[Install]
WantedBy=multi-user.target
EOF

log "Reloading systemd and starting service..."
systemctl daemon-reload
systemctl enable --now misaka-danmu

log "Waiting 5s for service to warm up..."
sleep 5

log "Checking service status and port..."
systemctl --no-pager -l status misaka-danmu || true
ss -ltnp | grep ${APP_PORT} || true

log "All done. Visit http://<server-ip>:${APP_PORT}"
log "Login with admin / Y2G8WjtLDsP#lpJ#"

