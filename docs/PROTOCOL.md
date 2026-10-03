# gattway protocol

Version 1. gattway lends a host's Bluetooth Low Energy radios to programs on the network. A client connects over
a WebSocket, asks a radio to scan, connects to a device, writes to and reads from its characteristics, and
receives its notifications. gattway knows nothing about what the devices are.

## 1. Transport

- WebSocket, text frames, one JSON object per frame. Default endpoint `ws://<host>:7120/ws`.
- Byte strings are lowercase hex (`"a1b2"`). UUIDs are full 128-bit lowercase strings; a 16-bit short form
  (`"ae3b"`) is accepted in requests and expanded to the Bluetooth base UUID.
- Times (`t`) are seconds on the instance's own clock (`time.time()` on the host). Clients learn the offset from
  heartbeat replies and from `status`.
- No authentication of its own. An instance may require a token: the client passes it as `?token=` in the URL.
  Run gattway on a trusted network, or put an authenticating reverse proxy in front.
- On connection the instance sends `hello`, then `status` on every change and at least every 5 s.

## 2. Requests and replies

Every request carries `id` (a string the client chooses) and `op`. Every request gets exactly one reply:

```json
{"type": "reply", "id": "7f3a", "ok": true, "result": {...}}
{"type": "reply", "id": "7f3a", "ok": false, "error": {"code": "held", "message": "connected by sensor-app", "data": {"owner": "sensor-app"}}}
```

Error codes: `bad_request`, `unknown_op`, `no_radio` (the named or default radio is not present or not
enabled), `not_found` (device not seen in a scan or not reachable), `connect_failed`, `held` (another client
owns the device; `data.owner`), `not_owner` (the device belongs to another client), `not_connected`,
`no_characteristic`, `write_failed`, `read_failed`, `timeout`, `busy` (the radio is scanning or connecting),
`unauthorised`.

| op | args | result |
|---|---|---|
| `hello` | `name` (how this client is named to others), `protocol` ("gattway"), `version` (1) | the instance's `hello` fields |
| `radios` | | `radios` as in `status` |
| `scan` | `radio?`, `timeout_s` (1 to 30, default 5), `name_prefix?`, `services?` (UUID list) | `{"devices": [{"address", "name", "rssi", "radio", "services": [...], "manufacturer": {"<company id>": "<hex>"}}]}`, strongest first |
| `connect` | `address`, `radio?`, `timeout_s?` (default 20) | `{"address", "name", "radio", "services": [{"uuid", "characteristics": [{"uuid", "properties": ["read", "write", "write-without-response", "notify", "indicate"]}]}]}` |
| `disconnect` | `address` | `{}` after the link is down. Farewell writes are **not** sent: the client has done its own stopping. |
| `farewell` | `address`, `writes` (list of `{"char", "data", "response"?}`) | `{}`; replaces earlier farewell writes for this device |
| `write` | `address`, `char`, `data`, `response?` (default false: write without response) | `{}` once written (and acknowledged, when `response` is true) |
| `write_at` | `address`, `char`, `response?`, `frames` (list of `{"t", "data"}` in the instance's clock) | `{"queued": n, "replaced": m, "late": k}` |
| `cancel` | `address`, `char?` | `{"cancelled": n}` pending timed frames removed |
| `read` | `address`, `char` | `{"data"}` |
| `subscribe` | `address`, `char` | `{}`; notifications follow as `notify` messages |
| `unsubscribe` | `address`, `char` | `{}` |

## 3. Messages from the instance

### 3.1 hello

Sent once, first.

```json
{"type": "hello", "protocol": "gattway", "version": 1, "t": 1727950000.123,
 "instance": {"name": "lab-pi", "host": "lab-pi", "software": "gattway 0.1.0"},
 "radios": [...], "devices": [...]}
```

### 3.2 status

Sent on every change and at least every 5 s. The whole state, so a client needs no history.

```json
{"type": "status", "t": 1727950005.001,
 "instance": {"name": "lab-pi", "host": "lab-pi", "software": "gattway 0.1.0"},
 "radios": [
   {"label": "usb", "address": "ac:a7:f1:b0:28:53", "hci": "hci1", "bus": "usb", "enabled": true, "default": true,
    "present": true, "powered": true, "blocked": false, "scanning": false},
   {"label": null, "address": "b8:27:eb:00:00:00", "hci": "hci0", "bus": "uart", "enabled": false, "default": false,
    "present": true, "powered": false, "blocked": true, "scanning": false}
 ],
 "devices": [
   {"address": "00:11:22:33:44:55", "name": "Widget-0001", "radio": "usb", "owner": "sensor-app",
    "connected_at": 1727949990.5, "subscribed": ["6e400003-b5a3-f393-e0a9-e50e24dcca9e"],
    "farewell": 0, "queue": {"pending": 0, "written": 0, "late": 0}}
 ],
 "clients": [{"name": "sensor-app", "connected_at": 1727949980.0}]}
```

Radios: every radio present on the host is listed, enabled or not, so an operator can see what exists. A
configured radio that is absent is listed with `present: false`. `label` is null for a radio that is present
but not configured.

### 3.3 notify

```json
{"type": "notify", "address": "00:11:22:33:44:55", "char": "6e400003-b5a3-f393-e0a9-e50e24dcca9e",
 "data": "aa aa 04 80 02 ...", "t": 1727950005.2345}
```

`data` is hex without spaces (the example is spaced for reading). `t` is when the instance received it.

### 3.4 disconnected

Sent to the owner when a device link ends for any reason, and to every client as part of the next `status`.

```json
{"type": "disconnected", "address": "00:11:22:33:44:55", "reason": "peer", "requested": false, "t": 1727950010.0}
```

`reason`: `requested` (the owner asked), `peer` (the device dropped the link), `owner_lost` (farewell ran), `radio`
(the radio went away), `error`.

### 3.5 hb

Answer to a client heartbeat: `{"type": "hb", "t": 1727950005.001, "echo": 1727950004.998}`. `echo` is the `t`
the client sent, if any, so the client can estimate round trip and clock offset.

## 4. Messages from a client

### 4.1 hb

```json
{"type": "hb", "t": 1727950004.998}
```

Every 2 s. See section 5.

### 4.2 requests

Section 2, as `{"id": "...", "op": "...", ...args}`.

## 5. Ownership, heartbeat and farewell

1. A connected device has exactly one **owner**: the client whose `connect` succeeded. Only the owner may
   `write`, `write_at`, `read`, `subscribe`, `farewell` and `disconnect` it. Everyone sees it in `status`.
2. `connect` to a device another client owns is refused with `held`. There is no takeover in version 1: the
   owner disconnects, or goes away.
3. A client is **lost** when its socket closes or when no `hb` has arrived for 6 s (three missed beats).
4. When the owner of a device is lost, the instance writes that device's **farewell** writes in order (each a
   write without response unless `response` is true, 20 ms apart, errors ignored), then disconnects the device,
   then reports `owner_lost`. A device with no farewell is simply disconnected. This is the only safety
   behaviour gattway has, and it is the client's job to register farewell writes that leave its device in a
   safe state (for example a zero-level frame followed by the device's stop command).
5. On shutdown (SIGTERM) the instance runs the farewell of every connected device and disconnects it.
6. A device that drops its own link (`peer`) is not reconnected by gattway. The owner decides.

## 6. Timed writes

`write_at` queues frames for one characteristic at absolute instance times. The instance writes each frame when
its time comes, from a loop that aims within a few milliseconds. A frame whose time has already passed by more
than `late_ms` (default 250) is dropped and counted as `late`; a frame with the same `t` as a queued one
replaces it. `cancel` clears the queue. `disconnect` and farewell clear it too.

The intended use: a client that must deliver a frame every 100 ms sends, every 100 ms, the next two or three
frames stamped ahead. Network jitter then costs nothing until it exceeds the depth of what was sent ahead.
Clients convert their clock to the instance's using the `hb` echo: `offset = t_instance - (t_sent + rtt / 2)`.

## 7. Radios

- The instance's configuration lists radios by BD address, each with a label, `enabled`, and at most one
  `default`. `scan` and `connect` take a radio label or address; omitted means the default.
- A configured radio that is not present gives `no_radio`. The instance never substitutes another radio.
- With no radios configured, every radio present is enabled and the first is the default. This is for trying
  gattway out; pin radios on a host that has more than one.
- Radios are identified by BD address because `hciN` numbering changes when a USB radio re-enumerates.
- Several radios may hold several devices at once. A device is held on one radio.

## 8. Versioning

`version` in `hello` is the protocol version. A client accepts the same major version. New ops, fields, error
codes and status fields may be added without a version change; a change in the meaning of an existing field
bumps it.

## 9. Example

```
instance -> {"type":"hello","protocol":"gattway","version":1,"instance":{...},"radios":[...],"devices":[]}
client   -> {"id":"1","op":"hello","name":"sensor-app","protocol":"gattway","version":1}
instance -> {"type":"reply","id":"1","ok":true,"result":{...}}
client   -> {"type":"hb","t":100.000}
instance -> {"type":"hb","t":200.004,"echo":100.000}
client   -> {"id":"2","op":"scan","timeout_s":5,"name_prefix":"Widget"}
instance -> {"type":"status",...,"radios":[{..."scanning":true}]}
instance -> {"type":"reply","id":"2","ok":true,"result":{"devices":[{"address":"00:11:22:33:44:55","name":"Widget-0001","rssi":-61,...}]}}
client   -> {"id":"3","op":"connect","address":"00:11:22:33:44:55"}
instance -> {"type":"reply","id":"3","ok":true,"result":{"address":"00:11:22:33:44:55","services":[...]}}
instance -> {"type":"status",...,"devices":[{"address":"00:11:22:33:44:55","owner":"sensor-app",...}]}
client   -> {"id":"4","op":"subscribe","address":"00:11:22:33:44:55","char":"6e400003-b5a3-f393-e0a9-e50e24dcca9e"}
instance -> {"type":"reply","id":"4","ok":true,"result":{}}
instance -> {"type":"notify","address":"00:11:22:33:44:55","char":"6e40...","data":"aaaa...","t":200.3}
...
client   -> {"id":"9","op":"disconnect","address":"00:11:22:33:44:55"}
instance -> {"type":"reply","id":"9","ok":true,"result":{}}
instance -> {"type":"disconnected","address":"00:11:22:33:44:55","reason":"requested","requested":true,"t":260.1}
```
