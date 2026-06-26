#!/usr/bin/env bash
# version 1.2.0
# First-run setup for a local ClipQueue Docker deployment.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"

set_env_value() {
  local key="$1"
  local value="$2"

  if grep -qE "^${key}=" "$ENV_FILE"; then
    sed -i -E "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    printf '\n%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: Docker is not installed or is not available in PATH." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: Docker Compose v2 is required." >&2
  exit 1
fi

mkdir -p "$PROJECT_DIR/media/input" "$PROJECT_DIR/media/output" "$PROJECT_DIR/data"

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$PROJECT_DIR/.env.example" "$ENV_FILE"
fi

set_env_value "INPUT_DIR" "$PROJECT_DIR/media/input"
set_env_value "OUTPUT_DIR" "$PROJECT_DIR/media/output"
set_env_value "DATA_DIR" "$PROJECT_DIR/data"
set_env_value "PUID" "$(id -u)"
set_env_value "PGID" "$(id -g)"

chmod -R u+rwX "$PROJECT_DIR/media/output" "$PROJECT_DIR/data"

echo "Starting ClipQueue..."
cd "$PROJECT_DIR"
docker compose up -d --build

echo
echo "ClipQueue is starting on: http://localhost:$(grep -E '^APP_PORT=' "$ENV_FILE" | cut -d= -f2 || printf '8097')"
echo "Place source videos in: $PROJECT_DIR/media/input"
