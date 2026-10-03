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
# Build from a throw-away copy: pip run as root would otherwise leave root-owned
# build/ and *.egg-info directories in the checkout, which the user owns.
BUILD_DIR=$(mktemp -d)
trap 'rm -rf "$BUILD_DIR"' EXIT
tar -C "$SRC_DIR" --exclude=.git --exclude=.venv --exclude=build --exclude='*.egg-info' -cf - . | tar -C "$BUILD_DIR" -xf -
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade "$BUILD_DIR"

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

# Distributions that ship a "bluetooth" group (Debian, Ubuntu, Raspberry Pi OS
# and others) grant BlueZ D-Bus access through it. Where there is no such group
# (Fedora, Arch and others) BlueZ's default policy applies and the unit must not
# name a group that does not exist, or systemd refuses to start it.
if getent group bluetooth >/dev/null 2>&1; then
    usermod -a -G bluetooth "$RUN_USER" || true
    GROUP_FILTER='s|^#BTGROUP ||'
else
    echo "no 'bluetooth' group on this host; the service runs with the user's own groups"
    GROUP_FILTER='/^#BTGROUP /d'
fi

sed -e "s|@USER@|$RUN_USER|g" -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" -e "s|@CONFIG@|$CONFIG|g" -e "$GROUP_FILTER" \
    "$SRC_DIR/deploy/gattway.service.in" > /etc/systemd/system/gattway.service

systemctl daemon-reload
systemctl enable gattway.service
systemctl restart gattway.service
echo "gattway installed. Status: systemctl status gattway; logs: journalctl -u gattway -f"
