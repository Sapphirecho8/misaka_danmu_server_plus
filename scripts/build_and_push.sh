#!/usr/bin/env bash
set -euo pipefail

IMAGE="sapphirecho8/misaka_danmu_server"
TAG="latest"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found; please install Docker first" >&2
  exit 1
fi

echo "Logging into Docker Hub..."
docker login || {
  echo "Login failed. Ensure your Docker Hub credentials are correct." >&2
  exit 1
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

echo "Building $IMAGE:$TAG with Dockerfile.custom..."
docker build -f Dockerfile.custom -t "$IMAGE:$TAG" .

echo "Pushing $IMAGE:$TAG..."
docker push "$IMAGE:$TAG"

echo "Done: $IMAGE:$TAG"

