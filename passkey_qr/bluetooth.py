"""Step 3: a local Bluetooth adapter, or the ha-ble-vhci bridge to a Home Assistant Bluetooth proxy."""

from __future__ import annotations

import getpass
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

from passkey_qr import core

BRIDGE_REPO = os.environ.get("PASSKEY_QR_BRIDGE_REPO", "https://github.com/alex-mextner/ha-ble-vhci")
BRIDGE_DIR = core.HOME / ".local/share/ha-ble-vhci"
BRIDGE_CFG = core.HOME / ".config/ha-ble-vhci"
BRIDGE_VERSION = "0.1.7"  # 0.1.7: --scanners exits 5 when Home Assistant is unreachable (0.1.6 said 1)
BRIDGE_UNIT = core.HOME / ".config/systemd/user/ha-ble-vhci.service"

UDEV_RULE = Path("/etc/udev/rules.d/99-vhci.rules")
UDEV_RULE_TEXT = 'KERNEL=="vhci", TAG+="uaccess", GROUP="plugdev", MODE="0660"\n'
MODULES_CONF = Path("/etc/modules-load.d/hci_vhci.conf")

BLUETOOTH_DIR = Path(os.environ.get("PASSKEY_QR_BLUETOOTH_DIR", "/sys/class/bluetooth"))


def bt_adapters() -> list[str]:
    return sorted(p.name for p in BLUETOOTH_DIR.glob("hci*")) if BLUETOOTH_DIR.exists() else []


def is_virtual(adapter: str) -> bool:
    return "/virtual/" in str((BLUETOOTH_DIR / adapter).resolve())


def virtual_adapter() -> str | None:
    return next((a for a in bt_adapters() if is_virtual(a)), None)


def real_adapters() -> list[str]:
    return [a for a in bt_adapters() if not is_virtual(a)]


def ha_url_configured() -> str | None:
    text = core.read_text(BRIDGE_CFG / "env") or ""
    match = re.search(r"^HA_URL=(\S+)", text, re.M)
    return match.group(1) if match else None


def _tailscale_hosts() -> list[str]:
    ts = core.out(["tailscale", "status", "--json"])
    if not ts:
        return []
    hosts = []
    try:
        for peer in (json.loads(ts).get("Peer") or {}).values():
            if peer.get("Online") and peer.get("TailscaleIPs"):
                hosts.append(peer["TailscaleIPs"][0])
    except json.JSONDecodeError:
        pass
    return hosts


def _is_home_assistant(host: str) -> bool:
    try:
        with socket.create_connection((host, 8123), timeout=1.5):
            return core.http(f"http://{host}:8123/api/") in (200, 401)
    except OSError:
        return False


def ha_candidates() -> list[str]:
    if os.environ.get("HA_URL"):
        return [os.environ["HA_URL"].rstrip("/")]
    hosts = ["homeassistant.local", *_tailscale_hosts()]
    return [f"http://{host}:8123" for host in hosts if _is_home_assistant(host)]


BRIDGE_BIN = BRIDGE_DIR / "venv/bin/ha-ble-vhci"


def version_tuple(version: str) -> tuple[tuple[int, ...], int]:
    """Orderable form of a PEP 440 release: '0.1.10' -> ((0, 1, 10), 1). Trailing zeros do not count ('0.2' equals
    '0.2.0'); a pre-/dev-release sorts before its release ('0.1.6rc1' < '0.1.6') and a post-release after it
    ('0.1.6.post1' > '0.1.6'); a '+local' part is ignored."""
    numbers: list[int] = []
    rank = 1  # 0: pre/dev release, 1: the release, 2: post release
    for piece in version.split("+")[0].split("."):
        match = re.fullmatch(r"(\d+)(.*)", piece)
        if match:
            numbers.append(int(match.group(1)))
            suffix = match.group(2)
        else:
            suffix = piece
        if suffix:
            rank = 2 if suffix.startswith("post") else 0
            break
    while numbers and numbers[-1] == 0:
        numbers.pop()
    return tuple(numbers), rank


def bridge_current() -> bool:
    pip = BRIDGE_DIR / "venv/bin/pip"
    if not (pip.exists() and BRIDGE_BIN.exists()):
        return False
    match = re.search(r"^Version: (\S+)", core.out([str(pip), "show", "ha-ble-vhci"]), re.M)
    return bool(match) and version_tuple(match.group(1)) >= version_tuple(BRIDGE_VERSION)  # newer is fine: never downgrade


def ha_valid() -> bool:
    url = ha_url_configured()
    token = BRIDGE_CFG / "token"
    if not (BRIDGE_BIN.exists() and url and token.exists()):
        return False
    try:
        return subprocess.run([str(BRIDGE_BIN), "--check", "--url", url, "--token-file", str(token)],
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def scanners_result() -> subprocess.CompletedProcess | None:
    """The bridge's ``--scanners`` answer; None when it cannot be asked (not set up) or timed out."""
    url = ha_url_configured()
    token = BRIDGE_CFG / "token"
    if not (BRIDGE_BIN.exists() and url and token.exists()):
        return None
    try:
        return subprocess.run([str(BRIDGE_BIN), "--scanners", "--url", url, "--token-file", str(token)],
                              capture_output=True, text=True, timeout=15)  # the bridge itself waits <= ~12 s
    except subprocess.TimeoutExpired:  # it connected to something that never answers: that is "unreachable"
        return subprocess.CompletedProcess([str(BRIDGE_BIN)], core.EXIT_UNREACHABLE, "",
                                           "timed out: Home Assistant did not answer within 15 s")
    except OSError:
        return None


def bridge_proxies() -> list[str] | None:
    """Names of the Bluetooth proxies online in Home Assistant right now (live, via the bridge's
    ``--scanners``); ``[]`` when none, ``None`` when it cannot be asked (not set up / unreachable)."""
    result = scanners_result()
    if result is None:
        return None
    if result.returncode == 4:
        return []
    if result.returncode != 0:
        return None
    return [line.split("  source=")[0] for line in result.stdout.splitlines() if line.strip()]


PROXY_HINT = ("power on / reconnect the ESP32 Bluetooth proxy; the QR dialog hangs silently without one "
              "(see journalctl --user -u ha-ble-vhci)")


def show_proxy_status() -> None:
    """Live, informational line: an offline proxy is transient, so it is never drift for ``check``/installer."""
    proxies = bridge_proxies()
    if proxies is None:
        return
    if proxies:
        core.say(f"  [x] a Bluetooth proxy is online in Home Assistant ({', '.join(proxies)})")
    else:
        core.say("  [ ] a Bluetooth proxy is online in Home Assistant\n      -> " + PROXY_HINT)


def bridge_unit_text() -> str:
    return (
        "[Unit]\nDescription=Home Assistant Bluetooth proxies as a local scan-only hciN\n"
        "After=network-online.target\nWants=network-online.target\n\n"
        f"[Service]\nEnvironmentFile=-{BRIDGE_CFG}/env\nExecStart={BRIDGE_BIN}\n"
        "Restart=on-failure\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n"
    )


def install_bridge() -> None:
    venv = BRIDGE_DIR / "venv"
    core.run([sys.executable, "-m", "venv", "--without-pip", str(venv)])
    if not core.DRY_RUN and not (venv / "bin/pip").exists():
        getpip = core.CACHE_DIR / "get-pip.py"
        getpip.parent.mkdir(parents=True, exist_ok=True)
        core.run(["curl", "-fsSL", "https://bootstrap.pypa.io/get-pip.py", "-o", str(getpip)])
        core.run([str(venv / "bin/python"), str(getpip), "-q"])
    core.run([str(venv / "bin/pip"), "install", "-q", "--upgrade", f"git+{BRIDGE_REPO}"])
    if core.service_active("ha-ble-vhci", user=True):
        core.run(["systemctl", "--user", "restart", "ha-ble-vhci"])  # pick up the new version


def ha_token() -> str | None:
    """A token that needs no question: ``$HA_TOKEN``, else an existing token file."""
    return os.environ.get("HA_TOKEN", "").strip() or (core.read_text(BRIDGE_CFG / "token") or "").strip() or None


def _hidden(prompt: str) -> str:
    core.log(f"{prompt}<hidden input, not logged>")
    return getpass.getpass(prompt).strip()


def _discover_ha_url() -> str:
    core.say("  Looking for Home Assistant (ESPHome Bluetooth proxy) on LAN / Tailscale ...")
    url = ""
    found = ha_candidates()
    if found:
        core.say("  found: " + ", ".join(found))
    if found and core.confirm(f"Use Home Assistant at {found[0]}?"):
        url = found[0]
    elif not core.DRY_RUN and not core.ASSUME_YES:
        url = core.ask("  Home Assistant URL (e.g. http://100.x.y.z:8123), Enter to abort: ").strip().rstrip("/")
    if not url:
        core.blocked("No Bluetooth adapter and no Home Assistant found: plug in a USB Bluetooth (BLE) dongle, "
                     "or set HA_URL and HA_TOKEN and re-run")
    return url


def _unattended_token(url: str, rejected: bool, stored: str) -> str:
    token = ha_token()
    if rejected and token == stored:
        token = None  # the stored token was just rejected; only a fresh $HA_TOKEN can fix it
    if not token:
        raise core.NeedsYou(f"Home Assistant at {url} needs a long-lived admin token: put it in $HA_TOKEN "
                            f"(or {BRIDGE_CFG / 'token'}) and re-run")
    core.say("  using the token from $HA_TOKEN or the token file; it is never printed")
    return token


def _prompted_token(url: str, rejected: bool) -> str:
    if rejected:
        core.say(f"  the stored token for {url} was rejected; enter a new one")
    core.say(f"  Create a long-lived token of an admin user: {url}/profile/security -> Long-lived access tokens")
    return "dry-run-token" if core.DRY_RUN else _hidden("  Paste the token (hidden, Enter to abort): ")


def configure_ha() -> None:
    url = ha_url_configured()
    stored = (core.read_text(BRIDGE_CFG / "token") or "").strip()
    if not url:
        url = _discover_ha_url()
        rejected = False
    else:
        rejected = bool(stored)
    if core.ASSUME_YES and core.DRY_RUN:
        token = "dry-run-token"  # the listing never asks for or prints a token
    elif core.ASSUME_YES:
        token = _unattended_token(url, rejected, stored)
    else:
        token = _prompted_token(url, rejected)
    if not token:
        raise core.StepError("no token given")
    core.write_file(BRIDGE_CFG / "token", token + "\n", 0o600)
    core.write_file(BRIDGE_CFG / "env", f"HA_URL={url}\n", 0o600)


def adapter_appears() -> None:
    if core.DRY_RUN:
        return
    for attempt in range(2):
        for _ in range(15):
            if virtual_adapter():
                return
            time.sleep(1)
        if attempt == 0:
            core.run(["systemctl", "--user", "restart", "ha-ble-vhci"])
    tail = core.out(["journalctl", "--user", "-u", "ha-ble-vhci", "-n", "15", "--no-pager"])
    raise core.StepError("the virtual adapter did not appear. Service log:\n" + tail)


def bluetooth_actions() -> list[core.Action]:
    return [
        core.Action(f"ha-ble-vhci {BRIDGE_VERSION} or newer installed", bridge_current, install_bridge),
        core.Action("Home Assistant URL + token stored", lambda: bool(ha_url_configured()) and (BRIDGE_CFG / "token").exists(), configure_ha),
        core.Action("Home Assistant reachable, token valid and admin", ha_valid, configure_ha,
                    hint="Check the URL/token (the token must belong to an admin user)."),
        core.Action("udev rule + hci_vhci autoload",
                    lambda: core.read_text(UDEV_RULE) == UDEV_RULE_TEXT and (core.read_text(MODULES_CONF) or "").strip() == "hci_vhci",
                    lambda: (core.sudo_write(UDEV_RULE, UDEV_RULE_TEXT), core.sudo_write(MODULES_CONF, "hci_vhci\n"))),
        core.Action("/dev/vhci exists (hci_vhci loaded)", lambda: Path("/dev/vhci").exists(),
                    lambda: core.run(["modprobe", "hci_vhci"], sudo=True)),
        core.Action("/dev/vhci usable by you", lambda: os.access("/dev/vhci", os.R_OK | os.W_OK),
                    lambda: (core.run(["udevadm", "control", "--reload"], sudo=True), core.run(["udevadm", "trigger"], sudo=True)),
                    hint="Log out and back in once (so the new device ACL / plugdev membership applies), then re-run."),
        core.Action("bluetooth.service running", lambda: core.service_active("bluetooth"),
                    lambda: core.run(["systemctl", "enable", "--now", "bluetooth"], sudo=True)),
        core.Action("ha-ble-vhci user service running",
                    lambda: BRIDGE_UNIT.exists() and core.service_active("ha-ble-vhci", user=True)
                    and core.out(["systemctl", "--user", "is-enabled", "ha-ble-vhci"]) == "enabled",
                    lambda: (core.write_file(BRIDGE_UNIT, bridge_unit_text()),
                             core.run(["systemctl", "--user", "daemon-reload"]),
                             core.run(["systemctl", "--user", "enable", "ha-ble-vhci"]),
                             core.run(["systemctl", "--user", "restart", "ha-ble-vhci"]))),
        core.Action("virtual Bluetooth adapter registered", lambda: virtual_adapter() is not None,
                    adapter_appears),
        core.Action("adapter powered on", lambda: "Powered: yes" in core.out(["bluetoothctl", "show"]),
                    lambda: core.run(["bluetoothctl", "power", "on"], check=False)),
    ]
