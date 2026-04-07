#!/bin/bash
# XDM Bot - Ubuntu 24.04 VPS Setup Script
# Run as root or with sudo: sudo bash deploy/setup.sh

set -e

APP_USER="${1:-ben}"
APP_DIR="/home/$APP_USER/xdm"
REPO_URL="https://github.com/Phasor/XDM.git"

echo "=== XDM Bot Setup for Ubuntu 24.04 ==="
echo "App user: $APP_USER"
echo "App directory: $APP_DIR"
echo ""

# --- System dependencies ---
echo "[1/6] Installing system dependencies..."
apt-get update
apt-get install -y \
    python3 python3-pip python3-venv \
    xvfb \
    wget unzip curl gnupg \
    libglib2.0-0 libnss3 libnspr4 libdbus-1-3 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdrm2 libxcomposite1 libxdamage1 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2t64 libxshmfence1

# --- Google Chrome ---
echo "[2/6] Installing Google Chrome..."
if ! command -v google-chrome &> /dev/null; then
    wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    apt-get install -y /tmp/chrome.deb || apt-get install -f -y
    rm /tmp/chrome.deb
    echo "Chrome installed: $(google-chrome --version)"
else
    echo "Chrome already installed: $(google-chrome --version)"
fi

# --- Clone repo ---
echo "[3/6] Setting up application..."
if [ -d "$APP_DIR" ]; then
    echo "Directory exists, pulling latest..."
    cd "$APP_DIR"
    sudo -u "$APP_USER" git pull
else
    sudo -u "$APP_USER" git clone "$REPO_URL" "$APP_DIR"
    cd "$APP_DIR"
fi

# --- Python venv ---
echo "[4/6] Setting up Python virtual environment..."
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# --- Config ---
echo "[5/6] Setting up configuration..."
if [ ! -f "$APP_DIR/config.json" ]; then
    sudo -u "$APP_USER" cp "$APP_DIR/config.template.json" "$APP_DIR/config.json"
    echo ""
    echo "  *** IMPORTANT: Edit $APP_DIR/config.json with your credentials ***"
    echo "  nano $APP_DIR/config.json"
    echo ""
else
    echo "config.json already exists, skipping."
fi

# --- Systemd service ---
echo "[6/6] Installing systemd service..."
cp "$APP_DIR/deploy/xdm.service" /etc/systemd/system/xdm.service
sed -i "s|__APP_USER__|$APP_USER|g" /etc/systemd/system/xdm.service
sed -i "s|__APP_DIR__|$APP_DIR|g" /etc/systemd/system/xdm.service
systemctl daemon-reload
systemctl enable xdm

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit config:        nano $APP_DIR/config.json"
echo "  2. Edit personality:   nano $APP_DIR/prompts/personality.txt"
echo "  3. Start the bot:      sudo systemctl start xdm"
echo "  4. Check status:       sudo systemctl status xdm"
echo "  5. View logs:          sudo journalctl -u xdm -f"
echo ""
