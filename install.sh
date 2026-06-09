#!/usr/bin/env sh
set -eu

printf '\nShellCrash 飞书机器人：镜像版安装向导\n'
printf '提示：本脚本会在当前目录生成 .env 和 docker-compose.yaml。\n'
printf '如果当前目录已有 .env，会先备份为 .env.bak.<时间戳>。\n\n'

command -v docker >/dev/null 2>&1 || {
  echo '错误：未找到 docker 命令，请先在 NAS 安装/启用 Docker。'
  exit 1
}

if docker compose version >/dev/null 2>&1; then
  COMPOSE='docker compose'
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE='docker-compose'
else
  echo '错误：未找到 docker compose 或 docker-compose。'
  exit 1
fi

read_default() {
  prompt="$1"
  default="$2"
  printf '%s [%s]: ' "$prompt" "$default"
  IFS= read -r value
  if [ -z "$value" ]; then
    value="$default"
  fi
  printf '%s' "$value"
}

read_secret() {
  prompt="$1"
  printf '%s: ' "$prompt"
  stty -echo 2>/dev/null || true
  IFS= read -r value
  stty echo 2>/dev/null || true
  printf '\n'
  printf '%s' "$value"
}

BOT_IMAGE="$(read_default '镜像地址' 'ghcr.io/rocoer7/shellcrash-feishu-bot:latest')"
FEISHU_APP_ID="$(read_default '飞书 App ID' '')"
FEISHU_APP_SECRET="$(read_secret '飞书 App Secret')"
ROUTER_HOST="$(read_default '路由器地址' '192.168.31.1')"
ROUTER_PORT="$(read_default 'SSH 端口' '22')"
ROUTER_USER="$(read_default 'SSH 用户' 'root')"
ROUTER_PASSWORD="$(read_secret 'SSH 密码（如果使用密钥可留空，直接回车）')"
ALLOW_ALL_USERS="$(read_default '是否临时允许所有用户测试 true/false' 'false')"
ALLOWED_OPEN_IDS="$(read_default '允许的飞书 open_id（可先留空，之后用 /whoami 获取）' '')"

mkdir -p keys

if [ -f .env ]; then
  cp .env ".env.bak.$(date +%Y%m%d%H%M%S)"
fi

cat > .env <<ENVEOF
BOT_IMAGE=$BOT_IMAGE
CONTAINER_NAME=shellcrash-feishu-bot
FEISHU_APP_ID=$FEISHU_APP_ID
FEISHU_APP_SECRET=$FEISHU_APP_SECRET
ROUTER_HOST=$ROUTER_HOST
ROUTER_PORT=$ROUTER_PORT
ROUTER_USER=$ROUTER_USER
ROUTER_PASSWORD=$ROUTER_PASSWORD
SSH_KEY_PATH=
ALLOW_ALL_USERS=$ALLOW_ALL_USERS
ALLOWED_OPEN_IDS=$ALLOWED_OPEN_IDS
ALLOWED_CHAT_IDS=
MAX_REPLY_CHARS=3200
LOG_LEVEL=INFO
ENVEOF
chmod 600 .env 2>/dev/null || true

cat > docker-compose.yaml <<'YAMLEOF'
services:
  shellcrash-feishu-bot:
    image: ${BOT_IMAGE:-ghcr.io/rocoer7/shellcrash-feishu-bot:latest}
    container_name: ${CONTAINER_NAME:-shellcrash-feishu-bot}
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - ./keys:/app/keys:ro
YAMLEOF

printf '\n开始拉取并启动容器...\n'
$COMPOSE pull
$COMPOSE up -d

printf '\n已启动。查看日志：\n'
printf '  %s logs -f shellcrash-feishu-bot\n\n' "$COMPOSE"
printf '飞书里先发送：/whoami、/status、/menu\n'
printf '注意：.env 是隐藏文件，绿联/部分 NAS 文件管理器可能看不到，请用 SSH 修改。\n'
