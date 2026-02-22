#!/usr/bin/env bash
# 一键构建并推送 TradingAgents-CN 前后端镜像到 Docker Hub
# 默认用户名: chenjunming123
#
# 用法:
#   ./scripts/push-dockerhub-chenjunming123.sh
#   VERSION=v1.0.1 ./scripts/push-dockerhub-chenjunming123.sh
#   VERSION=v1.0.1 PLATFORMS=linux/amd64 ./scripts/push-dockerhub-chenjunming123.sh
#   DOCKERHUB_USERNAME=yourname ./scripts/push-dockerhub-chenjunming123.sh

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DOCKERHUB_USERNAME="${DOCKERHUB_USERNAME:-chenjunming123}"
VERSION="${VERSION:-$(cat VERSION 2>/dev/null || echo v1.0.0-preview)}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
BUILDER_NAME="${BUILDER_NAME:-tradingagents-builder}"
CACHE_ENABLED="${CACHE_ENABLED:-true}"
CACHE_MODE="${CACHE_MODE:-max}"
CACHE_IMAGE="${CACHE_IMAGE:-${DOCKERHUB_USERNAME}/tradingagents-buildcache}"

echo "========================================"
echo "TradingAgents-CN Docker Hub 发布"
echo "========================================"
echo "Docker Hub 用户: ${DOCKERHUB_USERNAME}"
echo "版本: ${VERSION}"
echo "平台: ${PLATFORMS}"
echo "缓存开关: ${CACHE_ENABLED}"
echo "缓存镜像: ${CACHE_IMAGE}"
echo

if ! command -v docker >/dev/null 2>&1; then
  echo "错误: 未检测到 docker 命令"
  exit 1
fi

if ! docker buildx version >/dev/null 2>&1; then
  echo "错误: 当前 Docker 不支持 buildx"
  exit 1
fi

echo "[1/4] 登录 Docker Hub..."
docker login -u "${DOCKERHUB_USERNAME}"

echo "[2/4] 准备 buildx builder..."
if ! docker buildx inspect "${BUILDER_NAME}" >/dev/null 2>&1; then
  docker buildx create --name "${BUILDER_NAME}" --driver docker-container --use
fi
docker buildx use "${BUILDER_NAME}"
docker buildx inspect --bootstrap >/dev/null

BACKEND_IMAGE="${DOCKERHUB_USERNAME}/tradingagents-backend"
FRONTEND_IMAGE="${DOCKERHUB_USERNAME}/tradingagents-frontend"

build_and_push() {
  local dockerfile="$1"
  local image="$2"
  local cache_ref="$3"

  local -a cache_args=()
  if [ "${CACHE_ENABLED}" = "true" ]; then
    cache_args=(
      --cache-from "type=registry,ref=${CACHE_IMAGE}:${cache_ref}"
      --cache-to "type=registry,ref=${CACHE_IMAGE}:${cache_ref},mode=${CACHE_MODE}"
    )
  fi

  docker buildx build \
    --platform "${PLATFORMS}" \
    -f "${dockerfile}" \
    -t "${image}:${VERSION}" \
    -t "${image}:latest" \
    "${cache_args[@]}" \
    --push \
    .
}

echo "[3/4] 构建并推送后端镜像..."
build_and_push "Dockerfile.backend" "${BACKEND_IMAGE}" "backend-cache"

echo "[4/4] 构建并推送前端镜像..."
build_and_push "Dockerfile.frontend" "${FRONTEND_IMAGE}" "frontend-cache"

echo
echo "发布完成:"
echo "  ${BACKEND_IMAGE}:${VERSION}"
echo "  ${BACKEND_IMAGE}:latest"
echo "  ${FRONTEND_IMAGE}:${VERSION}"
echo "  ${FRONTEND_IMAGE}:latest"
echo
echo "验证命令:"
echo "  docker buildx imagetools inspect ${BACKEND_IMAGE}:${VERSION}"
echo "  docker buildx imagetools inspect ${FRONTEND_IMAGE}:${VERSION}"
