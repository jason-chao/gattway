# Deploying gattway as a service

gattway runs on any Linux host with BlueZ and a Bluetooth Low Energy adapter:
a desktop or laptop, a server, a mini PC, or a single-board computer such as a
Raspberry Pi. This guide installs it as a systemd service.

## Requirements

- Linux with BlueZ 5 (`bluetoothd` running) and a Bluetooth adapter that
  supports LE. Built-in radios work; a USB adapter is often more reliable and
  easier to place near the devices.
- Python 3.11 or newer with `venv`.
- systemd, for the service. Without it, run `gattway --config ...` under any
  supervisor you like.

## 1. Packages

| Distribution | Command |
|---|---|
| Debian, Ubuntu, Raspberry Pi OS | `sudo apt install -y python3 python3-venv bluez rfkill git` |
| Fedora | `sudo dnf install -y python3 bluez git util-linux` |
| Arch Linux | `sudo pacman -S --needed python bluez bluez-utils git util-linux` and `sudo systemctl enable --now bluetooth` |

Other distributions need the same pieces: Python with `venv`, BlueZ and its
daemon, and optionally `rfkill`.

gattway reads radio addresses from sysfs and from BlueZ over D-Bus. It also
uses `hciconfig` when present, but does not need it (many distributions no
longer ship it). `rfkill` is only used to report blocked radios.

## 2. Get the code and install

```sh
git clone https://github.com/jason-chao/gattway.git
cd gattway
sudo deploy/install.sh            # /opt/gattway, runs as the invoking user
```

`install.sh` makes a venv in `/opt/gattway/.venv`, installs the package,
copies `gattway.example.toml` to `/etc/gattway/gattway.toml` if there is none,
renders `deploy/gattway.service.in` into `/etc/systemd/system/gattway.service`,
enables and starts it. Run it again after `git pull` to upgrade.

To choose a different directory or user: `sudo deploy/install.sh /srv/gattway someuser`.

**Access to BlueZ.** Where the distribution has a `bluetooth` group (Debian,
Ubuntu, Raspberry Pi OS and others), `install.sh` adds the service user to it
and the unit runs with it. Where there is none (Fedora, Arch and others), the
unit runs with the user's own groups and BlueZ's default D-Bus policy applies.
If gattway then reports permission errors, see Troubleshooting.

## 3. Pin your radios

Find the radios and their addresses:

```sh
bluetoothctl list                 # address of every controller
ls -l /sys/class/bluetooth/       # hciN names and whether each is USB or built in
```

Edit `/etc/gattway/gattway.toml`. Pin each radio by BD address: a USB radio's
`hciN` number changes when it re-enumerates, its address does not.

```toml
[instance]
name = "radio-host"

[[radio]]
label = "usb"
address = "00:11:22:33:44:55"
enabled = true
default = true

[[radio]]
label = "builtin"
address = "00:11:22:33:44:66"
enabled = false
```

Then `sudo systemctl restart gattway`.

With no `[[radio]]` entries, every radio present is enabled and the first is
the default. That is fine for trying it out on a host with one radio.

## 4. Radio power and rfkill

`ExecStartPre` runs `prepare-radios.sh`, which clears the rfkill soft block and
powers on **only** the radios the config enables (through BlueZ over D-Bus,
`hciconfig`, `btmgmt` or `bluetoothctl`, whichever is available). Disabled
radios are left as they are, so a built-in radio you do not want can stay
blocked:

```sh
sudo rfkill block $(rfkill -n -o ID,DEVICE | awk '$2=="hci0"{print $1}')
```

## 5. Token

Set a token so only your own programs can use the radios:

```sh
sudo sh -c 'echo GATTWAY_TOKEN=change-me >> /opt/gattway/gattway.env'
sudo systemctl restart gattway
```

Clients pass it as `?token=change-me` in the URL. The socket is not encrypted;
keep gattway on a trusted network or put a TLS reverse proxy in front.

## 6. Check it

```sh
systemctl status gattway
journalctl -u gattway -f
/opt/gattway/.venv/bin/gattway-cli ws://127.0.0.1:7120/ws radios
/opt/gattway/.venv/bin/gattway-cli ws://127.0.0.1:7120/ws scan --timeout 5
```

## 7. Shutdown behaviour

On `systemctl stop gattway` (SIGTERM) the instance runs the farewell writes of
every connected device, disconnects them, then exits. `TimeoutStopSec=20`
leaves room for that.

## Troubleshooting

- **A radio is missing from `radios`.** gattway lists a radio only when it can
  read its address, from sysfs or from BlueZ. Check that `bluetoothd` is
  running (`systemctl status bluetooth`) and that `bluetoothctl list` shows it.
- **`present: false` for a configured radio.** The address in the config does
  not match any radio on the host. Compare with `bluetoothctl list`.
- **`powered: false`.** `prepare-radios.sh` could not power it on. Try
  `bluetoothctl power on` by hand (after `select <address>` when there are
  several radios) and read the error, and check `rfkill list`.
- **`connect_failed` with `le-connection-abort-by-local` or `InProgress`.** A
  stale connection in BlueZ. gattway already runs `bluetoothctl disconnect
  <addr>` before connecting and retries; if it persists,
  `sudo systemctl restart bluetooth`.
- **Permission errors talking to D-Bus.** On distributions with a `bluetooth`
  group, make sure the service user is in it (`install.sh` adds it). Elsewhere,
  either run the service as a user BlueZ's policy allows, or add a D-Bus policy
  file under `/etc/dbus-1/system.d/` granting the service user access to
  `org.bluez`, then restart `dbus` and `bluetooth`.
- **Some built-in radios are slow.** Built-in radios on a shared UART (common
  on single-board computers) can deliver noticeably less throughput than a USB
  adapter. If a streaming device loses data, try a USB adapter and pin it.
