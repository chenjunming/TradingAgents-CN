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

echo "========================================"
echo "TradingAgents-CN Docker Hub 发布"
echo "========================================"
echo "Docker Hub 用户: ${DOCKERHUB_USERNAME}"
echo "版本: ${VERSION}"
echo "平台: ${PLATFORMS}"
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

echo "[3/4] 构建并推送后端镜像..."
docker buildx build \
  --platform "${PLATFORMS}" \
  -f Dockerfile.backend \
  -t "${BACKEND_IMAGE}:${VERSION}" \
  -t "${BACKEND_IMAGE}:latest" \
  --push \
  .

echo "[4/4] 构建并推送前端镜像..."
docker buildx build \
  --platform "${PLATFORMS}" \
  -f Dockerfile.frontend \
  -t "${FRONTEND_IMAGE}:${VERSION}" \
  -t "${FRONTEND_IMAGE}:latest" \
  --push \
  .

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
