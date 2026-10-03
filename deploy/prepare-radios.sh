#!/bin/sh
# Unblock (rfkill) and power on only the radios the gattway config enables.
# Usage: prepare-radios.sh [CONFIG]   (default: $GATTWAY_CONFIG or /etc/gattway/gattway.toml)
# Runs as root from ExecStartPre. Never fails the unit: a missing radio is
# reported by gattway itself as present=false.
set -u

CONFIG="${1:-${GATTWAY_CONFIG:-/etc/gattway/gattway.toml}}"
PYTHON="${PYTHON:-$(dirname "$0")/.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON=python3

if [ ! -r "$CONFIG" ]; then
    echo "prepare-radios: no config at $CONFIG; leaving radios alone" >&2
    exit 0
fi

# Enabled radio addresses, lowercase, one per line. With no [[radio]] table
# gattway enables every radio, so power them all on.
ADDRESSES=$("$PYTHON" - "$CONFIG" <<'PY'
import sys, tomllib
with open(sys.argv[1], "rb") as fh:
    cfg = tomllib.load(fh)
radios = cfg.get("radio") or []
if not radios:
    print("*")
for r in radios:
    if r.get("enabled", True) and r.get("address"):
        print(str(r["address"]).lower())
PY
) || exit 0

hci_for_address() {
    # Print the hciN whose BD address matches $1, using sysfs, then BlueZ, then hciconfig.
    for d in /sys/class/bluetooth/hci*; do
        [ -e "$d" ] || continue
        if [ -r "$d/address" ] && [ "$(tr 'A-Z' 'a-z' < "$d/address")" = "$1" ]; then
            basename "$d"; return 0
        fi
    done
    # BlueZ over D-Bus (busctl ships with systemd), then the deprecated hciconfig.
    if command -v busctl >/dev/null 2>&1; then
        for d in /sys/class/bluetooth/hci*; do
            [ -e "$d" ] || continue
            h=$(basename "$d")
            a=$(busctl get-property org.bluez "/org/bluez/$h" org.bluez.Adapter1 Address 2>/dev/null \
                | awk '{ gsub(/"/, "", $2); print tolower($2) }')
            if [ "$a" = "$1" ]; then echo "$h"; return 0; fi
        done
    fi
    if command -v hciconfig >/dev/null 2>&1; then
        hciconfig -a 2>/dev/null | awk -v want="$1" '
            /^hci[0-9]+:/ { hci = substr($1, 1, length($1) - 1) }
            /BD Address:/ { if (tolower($3) == want) { print hci; exit } }'
    fi
}

power_on() {
    hci="$1"
    idx="${hci#hci}"
    if command -v rfkill >/dev/null 2>&1; then
        # Unblock just this radio: find its rfkill id by device name.
        ids=$(rfkill -n -o ID,DEVICE 2>/dev/null | awk -v want="$hci" '$2 == want { print $1 }')
        for id in $ids; do rfkill unblock "$id" 2>/dev/null; done
    fi
    if command -v busctl >/dev/null 2>&1; then
        busctl set-property org.bluez "/org/bluez/$hci" org.bluez.Adapter1 Powered b true 2>/dev/null && return 0
    fi
    if command -v hciconfig >/dev/null 2>&1; then
        hciconfig "$hci" up 2>/dev/null && return 0
    fi
    if command -v btmgmt >/dev/null 2>&1; then
        btmgmt --index "$idx" power on >/dev/null 2>&1 && return 0
    fi
    if command -v bluetoothctl >/dev/null 2>&1; then
        addr=$(cat "/sys/class/bluetooth/$hci/address" 2>/dev/null)
        [ -n "$addr" ] && printf 'select %s\npower on\n' "$addr" | bluetoothctl >/dev/null 2>&1
    fi
}

if [ "$ADDRESSES" = "*" ]; then
    for d in /sys/class/bluetooth/hci*; do
        [ -e "$d" ] && power_on "$(basename "$d")"
    done
    exit 0
fi

for addr in $ADDRESSES; do
    hci=$(hci_for_address "$addr")
    if [ -z "$hci" ]; then
        echo "prepare-radios: radio $addr is not present" >&2
        continue
    fi
    power_on "$hci"
done
exit 0
