# Deploying gattway on a Raspberry Pi

This covers a Pi running Raspberry Pi OS (Debian 12 or newer, Python 3.11+)
with BlueZ. Any Debian-like Linux host with BlueZ works the same way.

## 1. Packages

```sh
sudo apt update
sudo apt install -y python3 python3-venv bluez rfkill git
```

`bluez` provides `bluetoothd`, `bluetoothctl` and (on most builds) `hciconfig`.
gattway uses `hciconfig -a` to read BD addresses when sysfs does not expose
them, and `rfkill` to report blocked radios. Both are optional.

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

To choose a different directory or user: `sudo deploy/install.sh /srv/gattway pi`.

## 3. Pin your radios

Find the radios and their addresses:

```sh
hciconfig -a            # or: bluetoothctl list
ls -l /sys/class/bluetooth/
```

Edit `/etc/gattway/gattway.toml`. Pin each radio by BD address; a USB radio's
`hciN` number changes when it re-enumerates, its address does not.

```toml
[instance]
name = "lab-pi"

[[radio]]
label = "usb"
address = "00:11:22:33:44:55"
enabled = true
default = true

[[radio]]
label = "onboard"
address = "00:11:22:33:44:66"
enabled = false
```

Then `sudo systemctl restart gattway`.

With no `[[radio]]` entries, every radio present is enabled and the first is
the default. That is fine for trying it out on a Pi with one radio.

## 4. Radio power and rfkill

`ExecStartPre` runs `prepare-radios.sh`, which clears the rfkill soft block and
powers on **only** the radios the config enables. Disabled radios are left as
they are, so an onboard radio you do not want can stay blocked:

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

- `present: false` for a configured radio: the address in the config does not
  match any radio on the host. Compare with `hciconfig -a`.
- `powered: false`: `prepare-radios.sh` could not power it on. Try
  `sudo hciconfig hci0 up` or `bluetoothctl power on` by hand and read the error.
- `connect_failed` with `le-connection-abort-by-local` or `InProgress`: a stale
  connection in BlueZ. gattway already runs `bluetoothctl disconnect <addr>`
  before connecting and retries; if it persists, `sudo systemctl restart bluetooth`.
- Permission errors talking to D-Bus: make sure the service user is in the
  `bluetooth` group (`install.sh` adds it) and log out and in again.
