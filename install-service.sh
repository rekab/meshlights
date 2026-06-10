#!/bin/bash
# install-service.sh — install Meshlights as a systemd service.
#
# Generates /etc/systemd/system/meshlights.service from the current source
# checkout (whoever's running this, wherever the repo lives), then enables
# it to start at boot.
#
# Usage:  sudo ./install-service.sh
#
# After install:
#   sudo systemctl start meshlights        # start now
#   sudo systemctl status meshlights       # check status
#   journalctl -u meshlights -f            # tail logs
#   sudo systemctl restart meshlights      # pick up code changes
#
# To uninstall:
#   sudo systemctl disable --now meshlights
#   sudo rm /etc/systemd/system/meshlights.service
#   sudo systemctl daemon-reload

set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "Run with sudo: sudo $0" >&2
    exit 1
fi

# Identify the invoking (non-root) user. sudo sets SUDO_USER; refuse to
# install on behalf of a real root login so the service doesn't run as root.
USER_NAME="${SUDO_USER:-}"
if [ -z "$USER_NAME" ] || [ "$USER_NAME" = "root" ]; then
    echo "Don't run as a real root login — sudo from your normal user account." >&2
    exit 1
fi

USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)
WORKING_DIR=$(realpath "$(dirname "$0")")

# Locate uv in the invoking user's PATH (it's normally under ~/.local/bin/uv,
# which is NOT on root's PATH).
UV_PATH=$(sudo -u "$USER_NAME" -H bash -lc 'command -v uv' || true)
if [ -z "$UV_PATH" ]; then
    echo "uv not found for user '$USER_NAME'. Install it first:" >&2
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi

# Optional override: PORT=/dev/ttyACM1 sudo ./install-service.sh
PORT="${PORT:-/dev/ttyACM0}"

UNIT_PATH=/etc/systemd/system/meshlights.service

cat > "$UNIT_PATH" <<EOF
[Unit]
Description=Meshlights — MeshCore RX → LED strip
After=multi-user.target
Wants=${PORT//\//-}.device

[Service]
Type=simple
User=$USER_NAME
Group=$USER_NAME
WorkingDirectory=$WORKING_DIR
ExecStart=$UV_PATH run python engine.py --port $PORT
Restart=on-failure
RestartSec=5
# Send stdout/stderr to journald — view with: journalctl -u meshlights
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

echo "Wrote $UNIT_PATH:"
echo "---"
cat "$UNIT_PATH"
echo "---"

systemctl daemon-reload
systemctl enable meshlights

echo
echo "Enabled at boot. Start now with:"
echo "  sudo systemctl start meshlights"
echo "Tail logs with:"
echo "  journalctl -u meshlights -f"
