#!/usr/bin/env sh
set -eu

IMAGE_NAME="${1:-}"
VERSION="${2:-v1.0.0}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"

if [ -z "$IMAGE_NAME" ]; then
  echo "用法：$0 <镜像名> [版本号]"
  echo "示例：$0 ghcr.io/rocoer7/shellcrash-feishu-bot v1.0.0"
  exit 1
fi

command -v docker >/dev/null 2>&1 || {
  echo '错误：未找到 docker 命令。'
  exit 1
}

if ! docker buildx version >/dev/null 2>&1; then
  echo '错误：当前 Docker 不支持 buildx，无法构建多架构镜像。'
  exit 1
fi

# 创建 builder；如果已存在/已启用，不影响继续执行。
docker buildx create --use >/dev/null 2>&1 || true

echo "构建并推送：$IMAGE_NAME:latest 和 $IMAGE_NAME:$VERSION"
echo "平台：$PLATFORMS"

docker buildx build \
  --platform "$PLATFORMS" \
  -t "$IMAGE_NAME:latest" \
  -t "$IMAGE_NAME:$VERSION" \
  --push \
  .

echo '完成。'
