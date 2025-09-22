#!/usr/bin/env bash
set -euo pipefail

# CLI-driven non-interactive installer wrapper
# Examples:
# 1) 已有 MySQL 在 127.0.0.1:3333：
#    bash scripts/oneclick_noninteractive.sh -mysql y -mysqlhost 127.0.0.1 -mysqlport 3333 -mysqlpass bfl099yrSbmiwncc \
#      -hostport 8080 -jwtsecret KH7ig3khD2E04fFV -adminname akl -adminpass 'hLN$wHyuFO%qw^u0' \
#      -tmdb be3b5038a0dc0d4fd784a33e18af6690 -limit 200
# 2) 没有 MySQL，希望安装在 127.0.0.1:3333（Docker）：
#    bash scripts/oneclick_noninteractive.sh -mysql y -mysqlport 3333 -mysqlpass bfl099yrSbmiwncc \
#      -hostport 8080 -jwtsecret KH7ig3khD2E04fFV -adminname akl -adminpass 'hLN$wHyuFO%qw^u0' \
#      -tmdb be3b5038a0dc0d4fd784a33e18af6690 -limit 200

APP_DIR="/root/misaka_danmu_server"

# Defaults
USE_MYSQL="y"             # -mysql y|n
DB_HOST=""                # -mysqlhost
DB_PORT="3306"            # -mysqlport
DB_NAME="danmaku_db"
DB_USER="danmaku_user"
DB_PASS=""                # -mysqlpass (DB user password)
APP_PORT="7768"           # -hostport
JWT_SECRET=""             # -jwtsecret
ADMIN_USER="admin"        # -adminname
ADMIN_PASS=""             # -adminpass
TMDB_KEY=""               # -tmdb
RATE_LIMIT_PER_HOUR="1000" # -limit
MYSQL_ROOT_PWD="misaka_root_pwd"

# Parse args
while [ $# -gt 0 ]; do
  case "$1" in
    -mysql) shift; USE_MYSQL="${1:-y}" ;;
    -mysqlhost) shift; DB_HOST="${1:-}" ;;
    -mysqlport) shift; DB_PORT="${1:-3306}" ;;
    -mysqlpass) shift; DB_PASS="${1:-}" ;;
    -hostport) shift; APP_PORT="${1:-7768}" ;;
    -jwtsecret) shift; JWT_SECRET="${1:-}" ;;
    -adminname) shift; ADMIN_USER="${1:-admin}" ;;
    -adminpass) shift; ADMIN_PASS="${1:-}" ;;
    -tmdb) shift; TMDB_KEY="${1:-}" ;;
    -limit) shift; RATE_LIMIT_PER_HOUR="${1:-1000}" ;;
    *) echo "[warn] Unknown arg: $1" ;;
  esac
  shift || true
done

# Validate
[ -n "$DB_PASS" ] || { echo "[error] -mysqlpass required" >&2; exit 1; }
[ -n "$APP_PORT" ] || { echo "[error] -hostport required" >&2; exit 1; }
[ -n "$JWT_SECRET" ] || { echo "[error] -jwtsecret required" >&2; exit 1; }
[ -n "$ADMIN_USER" ] || { echo "[error] -adminname required" >&2; exit 1; }
[ -n "$ADMIN_PASS" ] || { echo "[error] -adminpass required" >&2; exit 1; }
[ -n "$TMDB_KEY" ] || { echo "[error] -tmdb required" >&2; exit 1; }

# Decide whether to install Docker MySQL
INSTALL_METHOD=2   # default Docker in interactive (1=apt,2=docker)
USE_EXISTING_MYSQL_ANSWER="Y"
DB_HOST_EFFECTIVE="$DB_HOST"
if [ "$USE_MYSQL" = "y" ] || [ "$USE_MYSQL" = "Y" ]; then
  if [ -z "$DB_HOST" ]; then
    # No host provided -> will install Docker MySQL on localhost mapped to DB_PORT
    USE_EXISTING_MYSQL_ANSWER="n"
    DB_HOST_EFFECTIVE="127.0.0.1"
  else
    USE_EXISTING_MYSQL_ANSWER="Y"
  fi
else
  # No MySQL requested -> install Docker and use localhost
  USE_EXISTING_MYSQL_ANSWER="n"
  DB_HOST_EFFECTIVE="127.0.0.1"
fi

# Build answers for the interactive script
ANS=$(mktemp)
{
  echo "$USE_EXISTING_MYSQL_ANSWER"
  echo "$DB_HOST_EFFECTIVE"
  echo "$DB_PORT"
  echo "$DB_NAME"
  echo "$DB_USER"
  echo "$DB_PASS"
  echo "$INSTALL_METHOD"
  echo "$MYSQL_ROOT_PWD"
  echo "$APP_PORT"
  echo "$JWT_SECRET"
  echo "$ADMIN_USER"
  echo "$ADMIN_PASS"
  echo "$TMDB_KEY"
  echo "$RATE_LIMIT_PER_HOUR"
} > "$ANS"

chmod +x "$APP_DIR/scripts/oneclick_interactive.sh"
bash "$APP_DIR/scripts/oneclick_interactive.sh" < "$ANS"

rm -f "$ANS"
echo "[oneclick] non-interactive install finished."
