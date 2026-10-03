"""Pure functions: UUIDs, hciconfig/rfkill parsing, config."""

import pytest

from gattway.config import ConfigError, apply_env, parse_config
from gattway.errors import GattwayError
from gattway.radios.hci import (
    bus_from_device_path,
    parse_hciconfig,
    parse_rfkill_json,
    parse_rfkill_list,
)
from gattway.uuids import expand_uuid, same_uuid

HCICONFIG_SAMPLE = """\
hci1:	Type: Primary  Bus: USB
	BD Address: 00:11:22:33:44:55  ACL MTU: 1021:8  SCO MTU: 64:1
	UP RUNNING
	RX bytes:1234 acl:0 sco:0 events:56 errors:0
	TX bytes:567 acl:0 sco:0 commands:45 errors:0
	Features: 0xff 0xff 0x8f 0xfe 0xdb 0xff 0x5b 0x87
	Packet type: DM1 DM3 DM5 DH1 DH3 DH5 HV1 HV2 HV3
	Link policy: RSWITCH SNIFF
	Link mode: PERIPHERAL ACCEPT
	Name: 'lab-pi'
	Class: 0x000000
	Service Classes: Unspecified
	Device Class: Miscellaneous,
	HCI Version: 5.0 (0x9)  Revision: 0x400
	LMP Version: 5.0 (0x9)  Subversion: 0x400
	Manufacturer: Cambridge Silicon Radio (10)

hci0:	Type: Primary  Bus: UART
	BD Address: 00:11:22:33:44:66  ACL MTU: 1021:8  SCO MTU: 64:1
	DOWN
	RX bytes:0 acl:0 sco:0 events:0 errors:0
	TX bytes:0 acl:0 sco:0 commands:0 errors:0

"""


def test_expand_uuid_short_forms():
    assert expand_uuid("ae3b") == "0000ae3b-0000-1000-8000-00805f9b34fb"
    assert expand_uuid("AE3B") == "0000ae3b-0000-1000-8000-00805f9b34fb"
    assert expand_uuid("0xae3b") == "0000ae3b-0000-1000-8000-00805f9b34fb"
    assert expand_uuid("0000ae3b") == "0000ae3b-0000-1000-8000-00805f9b34fb"
    full = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"
    assert expand_uuid(full) == full.lower()
    assert same_uuid("180f", "0000180F-0000-1000-8000-00805F9B34FB")


@pytest.mark.parametrize("bad", ["", "xyz", "ae3", "12345", "0000ae3b-0000", 42])
def test_expand_uuid_rejects_junk(bad):
    with pytest.raises(GattwayError) as info:
        expand_uuid(bad)
    assert info.value.code == "bad_request"


def test_parse_hciconfig():
    entries = parse_hciconfig(HCICONFIG_SAMPLE)
    assert set(entries) == {"hci0", "hci1"}
    assert entries["hci1"].address == "00:11:22:33:44:55"
    assert entries["hci1"].up is True
    assert entries["hci1"].bus == "usb"
    assert entries["hci0"].address == "00:11:22:33:44:66"
    assert entries["hci0"].up is False
    assert entries["hci0"].bus == "uart"


def test_parse_hciconfig_plain_output_without_flags():
    text = "hci0:\tType: Primary  Bus: USB\n\tBD Address: AA:BB:CC:DD:EE:FF  ACL MTU: 310:10  SCO MTU: 64:8\n"
    entries = parse_hciconfig(text)
    assert entries["hci0"].address == "aa:bb:cc:dd:ee:ff"
    assert entries["hci0"].up is None
    assert parse_hciconfig("") == {}


def test_parse_rfkill():
    modern = '{"rfkilldevices": [{"id": 0, "type": "wlan", "device": "phy0", "soft": "unblocked", "hard": "unblocked"},' \
             ' {"id": 1, "type": "bluetooth", "device": "hci0", "soft": "blocked", "hard": "unblocked"},' \
             ' {"id": 2, "type": "bluetooth", "device": "hci1", "soft": "unblocked", "hard": "unblocked"}]}'
    assert parse_rfkill_json(modern) == {"hci0": True, "hci1": False}
    old = '{"": [{"id": 1, "type": "bluetooth", "device": "hci0", "soft": "unblocked", "hard": "blocked"}]}'
    assert parse_rfkill_json(old) == {"hci0": True}
    assert parse_rfkill_json("not json") == {}
    listing = "0: phy0: Wireless LAN\n\tSoft blocked: no\n\tHard blocked: no\n1: hci0: Bluetooth\n\tSoft blocked: yes\n\tHard blocked: no\n"
    assert parse_rfkill_list(listing) == {"hci0": True}


def test_bus_from_device_path():
    assert bus_from_device_path("/sys/devices/platform/scb/fd500000.pcie/pci0000:00/0000:01:00.0/usb1/1-1/1-1.3/1-1.3:1.0") == "usb"
    assert bus_from_device_path("/sys/devices/platform/soc/fe201000.serial/serial0/serial0-0") == "uart"
    assert bus_from_device_path("/sys/devices/platform/soc/soc:bluetooth") == "uart"
    assert bus_from_device_path("/sys/devices/virtual/misc/vhci") is None


def test_config_defaults_when_nothing_configured():
    cfg = parse_config({})
    assert cfg.radios == []
    assert cfg.default_radio is None
    assert cfg.server.port == 7120
    assert cfg.server.host == "0.0.0.0"
    assert cfg.fake is False
    assert cfg.name  # hostname


def test_config_full():
    cfg = parse_config(
        {
            "instance": {"name": "lab"},
            "server": {"host": "127.0.0.1", "port": 7000, "token": "s3", "hb_timeout_s": 1.5},
            "radio": [
                {"label": "usb", "address": "00:11:22:33:44:55", "enabled": True, "default": True},
                {"label": "onboard", "address": "00:11:22:33:44:66", "enabled": False},
            ],
            "fake": {"enabled": True},
        }
    )
    assert cfg.name == "lab"
    assert (cfg.server.host, cfg.server.port, cfg.server.token) == ("127.0.0.1", 7000, "s3")
    assert cfg.server.hb_timeout_s == 1.5
    assert cfg.default_radio.label == "usb"
    assert cfg.radios[1].enabled is False and cfg.radios[1].default is False
    assert cfg.fake is True


def test_config_default_falls_back_to_first_enabled():
    cfg = parse_config({"radio": [{"address": "00:11:22:33:44:66", "enabled": False}, {"address": "00:11:22:33:44:55"}]})
    assert cfg.default_radio.address == "00:11:22:33:44:55"


@pytest.mark.parametrize(
    "data",
    [
        {"radio": [{"address": "a", "default": True}, {"address": "b", "default": True}]},
        {"radio": [{"address": "a"}, {"address": "A"}]},
        {"radio": [{"label": "x"}]},
        {"server": {"port": "many"}},
        {"server": {"port": 70000}},
    ],
)
def test_config_rejects(data):
    with pytest.raises(ConfigError):
        parse_config(data)


def test_env_overrides():
    cfg = parse_config({"server": {"token": "file"}})
    apply_env(cfg, {"GATTWAY_NAME": "env", "GATTWAY_HOST": "::", "GATTWAY_PORT": "8000", "GATTWAY_TOKEN": "", "GATTWAY_FAKE": "yes"})
    assert cfg.name == "env"
    assert cfg.server.host == "::"
    assert cfg.server.port == 8000
    assert cfg.server.token == ""
    assert cfg.fake is True
