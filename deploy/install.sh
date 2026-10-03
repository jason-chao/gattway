#!/bin/sh
# Install gattway as a systemd service on a Linux host with BlueZ.
#
#   sudo deploy/install.sh [INSTALL_DIR] [USER]
#
# INSTALL_DIR defaults to /opt/gattway, USER to the invoking user (SUDO_USER).
# The config lives at /etc/gattway/gattway.toml; an example is copied there if
# none exists. Re-run to upgrade: the venv is reused and the unit restarted.
set -eu

INSTALL_DIR="${1:-/opt/gattway}"
RUN_USER="${2:-${SUDO_USER:-$(id -un)}}"
CONFIG_DIR=/etc/gattway
CONFIG="$CONFIG_DIR/gattway.toml"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-python3}"

if [ "$(id -u)" -ne 0 ]; then
    echo "install.sh: run as root (sudo)" >&2
    exit 1
fi
if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    echo "install.sh: python 3.11 or newer is required ($PYTHON)" >&2
    exit 1
fi

echo "installing gattway from $SRC_DIR into $INSTALL_DIR (user $RUN_USER)"
mkdir -p "$INSTALL_DIR" "$CONFIG_DIR"

if [ ! -d "$INSTALL_DIR/.venv" ]; then
    "$PYTHON" -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade "$SRC_DIR"

install -m 0755 "$SRC_DIR/deploy/prepare-radios.sh" "$INSTALL_DIR/prepare-radios.sh"
if [ ! -f "$CONFIG" ]; then
    install -m 0644 "$SRC_DIR/gattway.example.toml" "$CONFIG"
    echo "wrote example config to $CONFIG; edit it to pin your radios"
fi
if [ ! -f "$INSTALL_DIR/gattway.env" ]; then
    printf '# Environment for gattway.service, e.g. GATTWAY_TOKEN=change-me\n' > "$INSTALL_DIR/gattway.env"
    chmod 0600 "$INSTALL_DIR/gattway.env"
fi
chown -R "$RUN_USER" "$INSTALL_DIR"

sed -e "s|@USER@|$RUN_USER|g" -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" -e "s|@CONFIG@|$CONFIG|g" \
    "$SRC_DIR/deploy/gattway.service.in" > /etc/systemd/system/gattway.service

# Membership of the bluetooth group lets the user talk to BlueZ over D-Bus on
# most distributions; the unit also adds it as a supplementary group.
if getent group bluetooth >/dev/null 2>&1; then
    usermod -a -G bluetooth "$RUN_USER" || true
fi

systemctl daemon-reload
systemctl enable gattway.service
systemctl restart gattway.service
echo "gattway installed. Status: systemctl status gattway; logs: journalctl -u gattway -f"
