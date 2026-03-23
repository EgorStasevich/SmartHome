#!/usr/bin/env sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  echo "Docker Compose не найден. Установи docker compose plugin или docker-compose."
  exit 1
fi

mkdir -p data

echo "Deploying assistant..."
$DC -f docker-compose.nas.yml up -d --build

echo "Service status:"
$DC -f docker-compose.nas.yml ps
