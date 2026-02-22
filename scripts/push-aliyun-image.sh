#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DEFAULT_REPO="crpi-ks35zqfhc6p9ox65.cn-shanghai.personal.cr.aliyuncs.com/cjm_stock/trading_agents"
DEFAULT_VERSION="$(tr -d '[:space:]' < VERSION 2>/dev/null || echo latest)"
DEFAULT_PLATFORMS="linux/amd64,linux/arm64"
DEFAULT_DOCKERFILE="Dockerfile.backend"
DEFAULT_CONTEXT="."
DEFAULT_BUILDER="tradingagents-aliyun-builder"
DEFAULT_CACHE_MODE="max"
DEFAULT_RETRY_WITHOUT_CACHE="true"
DEFAULT_PROVENANCE="false"
DEFAULT_SBOM="false"

usage() {
  cat <<'EOF'
Usage:
  # 从当前项目构建并推送到阿里云（多架构）
  ./scripts/push-aliyun-image.sh
  VERSION=v1.0.1 ./scripts/push-aliyun-image.sh
  VERSION=v1.0.1 PLATFORMS=linux/arm64 ./scripts/push-aliyun-image.sh

Environment variables (build mode):
  VERSION            default: VERSION 文件内容或 latest
  REPO               default: crpi-ks35zqfhc6p9ox65.cn-shanghai.personal.cr.aliyuncs.com/cjm_stock/trading_agents
  PLATFORMS          default: linux/amd64,linux/arm64
  DOCKERFILE         default: Dockerfile.backend
  CONTEXT            default: .
  BUILDER_NAME       default: tradingagents-aliyun-builder
  PUSH_LATEST        default: true (始终推 latest 标签)
  PUSH_VERSION_TAG   default: true (额外推 VERSION 标签)
  CACHE_ENABLED      default: true
  CACHE_IMAGE        default: <REPO>-buildcache
  CACHE_TAG          default: <dockerfile+platforms 生成>
  CACHE_MODE         default: max (min|max)
  RETRY_WITHOUT_CACHE default: true (缓存推送失败后自动重试一次，无缓存)
  PROVENANCE         default: false (ACR 兼容性更好)
  SBOM               default: false (ACR 兼容性更好)
  ACR_USERNAME       optional: 自动登录 ACR 用户名
  ACR_PASSWORD       optional: 自动登录 ACR 密码/令牌（建议用环境变量）

Examples:
  ./scripts/push-aliyun-image.sh
  VERSION=v1.0.0 ./scripts/push-aliyun-image.sh
  PLATFORMS=linux/arm64 ./scripts/push-aliyun-image.sh
  ACR_USERNAME=xxx ACR_PASSWORD=yyy ./scripts/push-aliyun-image.sh
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker not found"
  exit 1
fi

if [[ $# -gt 0 ]]; then
  echo "Error: this script no longer supports source-image retag/push mode."
  echo "It only builds from the current project and pushes to Aliyun ACR."
  echo "Run: ./scripts/push-aliyun-image.sh"
  exit 1
fi

normalize_bool() {
  case "${1,,}" in
    true|1|yes|y) echo "true" ;;
    false|0|no|n) echo "false" ;;
    *)
      return 1
      ;;
  esac
}

require_bool() {
  local key="$1"
  local value="$2"
  local normalized=""
  if ! normalized="$(normalize_bool "$value")"; then
    echo "Error: ${key} must be true/false (or 1/0, yes/no). current: ${value}"
    exit 1
  fi
  echo "$normalized"
}

ensure_docker_ready() {
  if ! docker info >/dev/null 2>&1; then
    echo "Error: docker daemon is not reachable. Please start Docker first."
    exit 1
  fi
}

ensure_buildx_builder() {
  local builder_name="$1"

  if ! docker buildx version >/dev/null 2>&1; then
    echo "Error: docker buildx is not available"
    exit 1
  fi

  if ! docker buildx inspect "$builder_name" >/dev/null 2>&1; then
    docker buildx create --name "$builder_name" --driver docker-container --use >/dev/null
  fi

  docker buildx use "$builder_name"
  docker buildx inspect --bootstrap >/dev/null
}

ensure_registry_login_if_credentials_provided() {
  local registry_host="$1"
  local username="${ACR_USERNAME:-${ALIYUN_USERNAME:-}}"
  local password="${ACR_PASSWORD:-${ALIYUN_PASSWORD:-}}"

  if [[ -n "$username" || -n "$password" ]]; then
    if [[ -z "$username" || -z "$password" ]]; then
      echo "Error: ACR_USERNAME and ACR_PASSWORD must be provided together."
      exit 1
    fi
    echo "Logging in to ${registry_host} with ACR_USERNAME..."
    if ! printf '%s' "$password" | docker login "$registry_host" -u "$username" --password-stdin >/dev/null; then
      echo "Error: docker login failed for ${registry_host}."
      exit 1
    fi
  else
    echo "No ACR credentials provided via env, using current docker login session."
  fi
}

run_buildx_push() {
  local use_cache="$1"
  local -a cache_args=()
  local -a metadata_args=()

  if [[ "$PROVENANCE" == "false" ]]; then
    metadata_args+=(--provenance=false)
  fi
  if [[ "$SBOM" == "false" ]]; then
    metadata_args+=(--sbom=false)
  fi

  if [[ "$use_cache" == "true" ]]; then
    cache_args=(
      --cache-from "type=registry,ref=${CACHE_IMAGE}:${CACHE_TAG}"
      --cache-to "type=registry,ref=${CACHE_IMAGE}:${CACHE_TAG},mode=${CACHE_MODE}"
    )
  fi

  docker buildx build \
    --platform "${PLATFORMS}" \
    -f "${DOCKERFILE}" \
    "${TAG_ARGS[@]}" \
    "${metadata_args[@]}" \
    "${cache_args[@]}" \
    --push \
    "${CONTEXT}"
}

# 默认模式：从当前项目构建并推送多架构镜像
VERSION="${VERSION:-$DEFAULT_VERSION}"
REPO="${REPO:-$DEFAULT_REPO}"
PLATFORMS="${PLATFORMS:-$DEFAULT_PLATFORMS}"
DOCKERFILE="${DOCKERFILE:-$DEFAULT_DOCKERFILE}"
CONTEXT="${CONTEXT:-$DEFAULT_CONTEXT}"
BUILDER_NAME="${BUILDER_NAME:-$DEFAULT_BUILDER}"
PUSH_LATEST="${PUSH_LATEST:-true}"
PUSH_VERSION_TAG="${PUSH_VERSION_TAG:-true}"
CACHE_ENABLED="${CACHE_ENABLED:-true}"
CACHE_MODE="${CACHE_MODE:-$DEFAULT_CACHE_MODE}"
CACHE_IMAGE="${CACHE_IMAGE:-${REPO}-buildcache}"
RETRY_WITHOUT_CACHE="${RETRY_WITHOUT_CACHE:-$DEFAULT_RETRY_WITHOUT_CACHE}"
PROVENANCE="${PROVENANCE:-$DEFAULT_PROVENANCE}"
SBOM="${SBOM:-$DEFAULT_SBOM}"

PUSH_LATEST="$(require_bool "PUSH_LATEST" "$PUSH_LATEST")"
PUSH_VERSION_TAG="$(require_bool "PUSH_VERSION_TAG" "$PUSH_VERSION_TAG")"
CACHE_ENABLED="$(require_bool "CACHE_ENABLED" "$CACHE_ENABLED")"
RETRY_WITHOUT_CACHE="$(require_bool "RETRY_WITHOUT_CACHE" "$RETRY_WITHOUT_CACHE")"
PROVENANCE="$(require_bool "PROVENANCE" "$PROVENANCE")"
SBOM="$(require_bool "SBOM" "$SBOM")"

DOCKERFILE_KEY="$(basename "$DOCKERFILE" | tr '.:/' '___')"
PLATFORMS_KEY="$(echo "$PLATFORMS" | tr ',/' '__')"
CACHE_TAG="${CACHE_TAG:-${DOCKERFILE_KEY}-${PLATFORMS_KEY}}"

if [[ ! -f "$DOCKERFILE" ]]; then
  echo "Error: dockerfile not found: $DOCKERFILE"
  exit 1
fi

if [[ ! -d "$CONTEXT" ]]; then
  echo "Error: build context not found: $CONTEXT"
  exit 1
fi

if [[ "$REPO" == *[A-Z]* ]]; then
  echo "Error: REPO must be lowercase for docker registry naming rules: ${REPO}"
  exit 1
fi

if [[ "$REPO" != */*/* ]]; then
  echo "Error: REPO must be in format <registry>/<namespace>/<repository>. current: ${REPO}"
  exit 1
fi

if [[ "$CACHE_ENABLED" == "true" && "$CACHE_MODE" != "min" && "$CACHE_MODE" != "max" ]]; then
  echo "Error: CACHE_MODE must be min or max. current: ${CACHE_MODE}"
  exit 1
fi

REGISTRY_HOST="${REPO%%/*}"
ensure_docker_ready

echo "========================================"
echo "Aliyun ACR Build & Push"
echo "========================================"
echo "Repo: ${REPO}"
echo "Version: ${VERSION}"
echo "Platforms: ${PLATFORMS}"
echo "Dockerfile: ${DOCKERFILE}"
echo "Context: ${CONTEXT}"
echo "Builder: ${BUILDER_NAME}"
echo "Cache enabled: ${CACHE_ENABLED}"
if [[ "$CACHE_ENABLED" == "true" ]]; then
  echo "Cache image: ${CACHE_IMAGE}"
  echo "Cache tag: ${CACHE_TAG}"
  echo "Cache mode: ${CACHE_MODE}"
fi
echo "Retry without cache: ${RETRY_WITHOUT_CACHE}"
echo "Provenance: ${PROVENANCE}"
echo "SBOM: ${SBOM}"
echo
echo "Tip: login command -> docker login ${REGISTRY_HOST} -u <acr-username>"
echo

ensure_registry_login_if_credentials_provided "${REGISTRY_HOST}"
ensure_buildx_builder "$BUILDER_NAME"

declare -a TAG_ARGS=()
if [[ "$PUSH_LATEST" == "true" ]]; then
  TAG_ARGS+=("-t" "${REPO}:latest")
fi
if [[ "$PUSH_VERSION_TAG" == "true" && "$VERSION" != "latest" ]]; then
  TAG_ARGS+=("-t" "${REPO}:${VERSION}")
fi
if [[ "${#TAG_ARGS[@]}" -eq 0 ]]; then
  echo "Error: no tags configured. Set PUSH_LATEST=true or PUSH_VERSION_TAG=true."
  exit 1
fi

echo "Building and pushing multi-arch image..."
BUILD_OK="false"
if run_buildx_push "$CACHE_ENABLED"; then
  BUILD_OK="true"
elif [[ "$CACHE_ENABLED" == "true" && "$RETRY_WITHOUT_CACHE" == "true" ]]; then
  echo
  echo "Build with registry cache failed. Retrying once without cache..."
  if run_buildx_push "false"; then
    BUILD_OK="true"
    CACHE_ENABLED="false"
  fi
fi

if [[ "$BUILD_OK" != "true" ]]; then
  echo "Error: buildx push failed."
  echo "If this is a permission/login issue, run: docker login ${REGISTRY_HOST}"
  echo "If this is ACR compatibility issue, keep PROVENANCE=false SBOM=false and try CACHE_ENABLED=false."
  exit 1
fi

echo
echo "Done."
echo "Published image:"
if [[ "$PUSH_LATEST" == "true" ]]; then
  echo "  ${REPO}:latest"
fi
if [[ "$PUSH_VERSION_TAG" == "true" && "$VERSION" != "latest" ]]; then
  echo "  ${REPO}:${VERSION}"
fi
echo
echo "Verify platforms:"
if [[ "$PUSH_VERSION_TAG" == "true" && "$VERSION" != "latest" ]]; then
  echo "  docker buildx imagetools inspect ${REPO}:${VERSION}"
fi
if [[ "$PUSH_LATEST" == "true" ]]; then
  echo "  docker buildx imagetools inspect ${REPO}:latest"
fi

if [[ "$PLATFORMS" != *"linux/arm64"* ]]; then
  echo
  echo "Warning: current PLATFORMS does not include linux/arm64."
  echo "Use: PLATFORMS=linux/arm64 or PLATFORMS=linux/amd64,linux/arm64"
fi

if [[ "$PLATFORMS" == *"linux/arm64"* ]]; then
  echo
  echo "ARM64 is included in this push."
fi

exit 0
