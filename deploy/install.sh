#!/bin/bash
set -euo pipefail
# Run on the GCP VM to install the bot as a systemd service.

REPO="https://github.com/pk1308/tds-p1-telegram-bot"
APP_DIR="/opt/tds-p1-telegram-bot"
USER="tdsbot"

# Create user and app directory.
sudo useradd -r -s /bin/false "$USER" || true
sudo mkdir -p "$APP_DIR"
sudo chown "$USER:$USER" "$APP_DIR"

# Install uv.
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# Clone or pull code.
if [ -d "$APP_DIR/.git" ]; then
    sudo -u "$USER" git -C "$APP_DIR" pull origin main
else
    sudo -u "$USER" git clone "$REPO" "$APP_DIR"
fi

# Install deps and sync.
cd "$APP_DIR"
sudo -u "$USER" uv sync --no-dev

# Write .env from environment variables set by the operator.
: "${TELEGRAM_BOT_TOKEN:?}"
: "${LLM_API_KEY:?}"
: "${GCS_LOG_BUCKET:?}"
sudo -u "$USER" tee "$APP_DIR/.env" >/dev/null <<EOF
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
LLM_BASE_URL=${LLM_BASE_URL:-https://openrouter.ai/api/v1}
LLM_API_KEY=${LLM_API_KEY}
LLM_MODEL=${LLM_MODEL:-openai/gpt-4o}
GCS_LOG_BUCKET=${GCS_LOG_BUCKET}
GCS_LOG_PREFIX=${GCS_LOG_PREFIX:-runs/}
EOF

# Install systemd service.
sudo cp "$APP_DIR/deploy/tds-p1-bot.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tds-p1-bot
sudo systemctl restart tds-p1-bot

echo "Bot deployed. Check status with: sudo systemctl status tds-p1-bot"
