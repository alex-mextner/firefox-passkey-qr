"""The health check, request watcher and prefs repair that `passkey-qr watch`, `health` and `heal-prefs` run."""

from __future__ import annotations

import argparse
import calendar
import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import traceback
import urllib.parse
from pathlib import Path
from typing import Callable, Iterator

from passkey_qr import bluetooth, core, credentialsd, extension, selfheal

HEALTH_STATE = core.STATE_DIR / "health.json"

HELPER_NAME = "credentialsd-firefox-helper"
CREDENTIALSD_UNITS = ("xyz.iinuwa.credentialsd.Credentials", "xyz.iinuwa.credentialsd.UiControl")

PORTAL_NAME = "org.freedesktop.portal.Desktop"
REQUEST_INTERFACE = "org.freedesktop.portal.experimental.Credential"
REGISTRY_INTERFACE = "org.freedesktop.host.portal.Registry"  # the helper registers its app id with this first
REQUEST_MEMBERS = ("CreateCredential", "GetCredential")  # a Firefox helper asking the portal for a passkey
REQUEST_MATCHES = (f"type='method_call',interface='{REQUEST_INTERFACE}'",
                   # only Firefox's id: other applications register with the portal all the time
                   f"type='method_call',interface='{REGISTRY_INTERFACE}',member='Register',arg0='{core.FIREFOX_APP_ID}'")
REQUEST_SETTLE_SECONDS = 5  # a portal call only counts when a Firefox helper is still alive this long after it
REQUEST_MARKER = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "passkey-qr/request-pending"
PASS_LOCK = REQUEST_MARKER.with_name("pass.lock")  # held by whoever runs a health pass
REQUEST_CHECK_SECONDS = 20  # while a request is pending the chain is re-checked this often
PROBE_RETRY_SECONDS = 3  # a failed Home Assistant query is repeated once before the user is told
HA_ACTION_TIMEOUT_SECONDS = 30  # docker start/unpause/restart over ssh: a pass waits this long at most for it
NOTIFY_MIN_SECONDS = 180  # the same failure is told at most this often, however many requests retry
EXIT_NO_SCANNER = 4  # exit codes of `ha-ble-vhci --scanners`: 0 online, 4 no proxy, 5 Home Assistant unreachable

HA_REPAIR_CONFIG = core.HOME / ".config/passkey-qr/ha-repair"  # HA_CONTAINER=<name> [HA_SSH_HOST=<ssh host>]
HA_BOOT_GRACE_SECONDS = 600  # a container started this recently is booting: Home Assistant needs minutes on a small board
HA_RESTART_POLICY_GRACE_SECONDS = 30  # a container that stopped this recently is about to be restarted by Docker
HA_HUNG_SECONDS = 240  # unreachable this long (continuously seen) with an unhealthy container: restart it
HA_DOWN_GAP_SECONDS = 90  # two sightings further apart than this are two outages
HA_INSPECT_COOLDOWN = 60  # the container is looked at over SSH at most this often
HA_REPAIR_COOLDOWN = 1800  # and repaired at most this often
ACTION_DONE = {"start": "started", "unpause": "unpaused", "restart": "restarted"}  # docker verb -> what the user is told
BRIDGE_SETTLE_SECONDS = 60  # a freshly started bridge gets this long to register its adapter
BRIDGE_RESTART_COOLDOWN = 300
RESTART_COOLDOWN = 300  # a repair that did not help (portal, credentialsd) is retried at most this often
MAX_PORTAL_RESTARTS = 3  # failed portal restarts in one outage before it is left alone (it only reports then)
SCANNER_STALE_SECONDS = 120  # the bridge itself says "no scanner" for this long while Home Assistant disagrees
STUCK_SECONDS = 600  # a helper process (one per WebAuthn request) older than this is an abandoned request


def journal_is_stdout() -> bool:
    """systemd's documented check: $JOURNAL_STREAM names the journal stream (dev:ino) our stdout is connected to."""
    try:
        stat = os.fstat(sys.stdout.fileno())
    except (OSError, ValueError):
        return False
    return os.environ.get("JOURNAL_STREAM") == f"{stat.st_dev}:{stat.st_ino}"


def hlog(message: str, level: int = 6) -> None:
    """A journal line; when stdout is the journal the <N> prefix sets its priority (4 = warning)."""
    core.say(f"<{level}>{message}" if journal_is_stdout() else message)


def notify(title: str, body: str, *, urgent: bool = False) -> None:
    hlog(f"notify: {title} - {body}", 4 if urgent else 6)
    if core.DRY_RUN:
        return
    exe = shutil.which("notify-send")
    if not exe:
        hlog("notify-send is not installed: the user cannot be told on the desktop", 4)
        return
    try:
        result = subprocess.run([exe, "-a", "passkey-qr", "-u", "critical" if urgent else "normal",
                                 "-i", "dialog-warning" if urgent else "dialog-information",
                                 "-h", "string:x-canonical-private-synchronous:passkey-qr",  # one box: the update replaces it
                                 title, body],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as err:
        hlog(f"notify-send failed: {err}", 4)
        return
    if result.returncode != 0:  # e.g. no notification daemon yet at an early login
        hlog(f"notify-send failed (exit {result.returncode}): {(result.stderr or '').strip()}", 4)


def load_health_state() -> dict:
    try:
        data = json.loads(HEALTH_STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_health_state(state: dict) -> None:
    if core.DRY_RUN:
        return
    try:
        HEALTH_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = HEALTH_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(HEALTH_STATE)
    except OSError as err:
        hlog(f"cannot save {HEALTH_STATE}: {err}", 4)


def may_act(state: dict, key: str, now: float, cooldown: float) -> bool:
    """True (and remembered) at most once per ``cooldown`` for the repair named ``key``."""
    acted = state.setdefault("acted", {})
    if 0 <= now - acted.get(key, 0) < cooldown:  # a clock set back counts as expired, not as "wait for it"
        return False
    acted[key] = now
    return True


def report(state: dict, key: str, ok: bool, now: float, *, up: tuple[str, str],
           down: tuple[str, str] | None = None) -> None:
    """The user hears about ``key`` only while a passkey request is pending (``state["pending"]``): once when it
    fails, never more often than every NOTIFY_MIN_SECONDS however often requests retry, and 'recovered' only for
    a request that was told about it. Without a pending request (a manual run) it is journal only."""
    entry = state.setdefault("issues", {}).setdefault(key, {})
    if ok:
        if entry.get("open"):
            notify(*up)
        elif entry.get("fails"):
            hlog(f"{key}: ok again")
        entry["open"], entry["fails"] = False, 0
        return
    assert down, "a failing report needs its notification text"
    entry["fails"] = entry.get("fails", 0) + 1
    hlog(f"{key}: FAILING ({entry['fails']}x): {down[0]}", 4)
    if not state.get("pending") or entry.get("open"):
        return
    entry["open"] = True  # this request knows about it, repeated or not: it hears when it is over
    told = entry.get("told")
    if told is not None and 0 <= now - told < NOTIFY_MIN_SECONDS:
        hlog(f"{key}: the user was told {now - told:.0f} s ago: not repeating")
        return
    notify(*down, urgent=True)
    entry["told"] = now


def session_ready() -> bool:
    """A graphical session: the systemd target is active, or (Xfce, i3, ... started without it) the user manager
    knows a display."""
    if core.out(["systemctl", "--user", "is-active", "graphical-session.target"]) == "active":
        return True
    return bool(re.search(r"^(WAYLAND_DISPLAY|DISPLAY)=", core.out(["systemctl", "--user", "show-environment"]), re.M))


def unit_age(unit: str) -> float:
    """Seconds since the unit became active (CLOCK_MONOTONIC, the clock systemd stamps with)."""
    stamp = core.out(["systemctl", "--user", "show", "-p", "ActiveEnterTimestampMonotonic", "--value", f"{unit}.service"])
    return time.monotonic() - int(stamp) / 1e6 if stamp.isdigit() and int(stamp) else 0.0


def scanner_log_says_none(now: float) -> bool:
    """Did the bridge say 'no Bluetooth scanner is connected' (journal, current run of the service only) longer
    than SCANNER_STALE_SECONDS ago, without a scanner coming online since? Per-scanner 'offline' lines are not
    enough: with two proxies one may be unplugged for good while the other works."""
    age = unit_age("ha-ble-vhci")
    if not age:
        return False
    text = core.out(["journalctl", "--user", "-u", "ha-ble-vhci", "-o", "short-unix", "--no-pager",
                     "--since", f"@{now - age:.0f}", "-n", "1", "-g", "no Bluetooth scanner|Bluetooth scanner online"])
    match = re.match(r"(\d+(?:\.\d+)?) \S+ \S+: (.*)", text.splitlines()[-1]) if text else None
    return bool(match) and "no Bluetooth scanner" in match.group(2) and now - float(match.group(1)) > SCANNER_STALE_SECONDS


def restart_unit(unit: str) -> None:
    core.run(["systemctl", "--user", "reset-failed", unit], check=False)
    core.run(["systemctl", "--user", "restart", unit], check=False)


def _bridge_settled(state: dict, now: float) -> bool:
    """Start or restart the bridge as needed; False while it has just (re)started and needs a while to register
    its adapter."""
    if not core.service_active("ha-ble-vhci", user=True):
        if may_act(state, "bridge-start", now, BRIDGE_RESTART_COOLDOWN):  # on top of systemd's own Restart=
            hlog("ha-ble-vhci is not running: starting it", 4)
            restart_unit("ha-ble-vhci")
        return False
    settled = unit_age("ha-ble-vhci") > BRIDGE_SETTLE_SECONDS
    if not bluetooth.bt_adapters() and settled and may_act(state, "bridge-restart", now, BRIDGE_RESTART_COOLDOWN):
        hlog("ha-ble-vhci is running but registered no Bluetooth adapter: restarting it", 4)
        restart_unit("ha-ble-vhci")
        for _ in range(15):
            if bluetooth.bt_adapters():
                break
            time.sleep(1)
        return False
    return settled


def _report_adapter(state: dict, now: float, needs_bridge: bool, settled: bool) -> None:
    up = ("Bluetooth adapter is back", "Passkey QR can find phones again.")
    if bluetooth.bt_adapters():
        shown = core.out(["bluetoothctl", "show"])
        if "Powered: yes" not in shown:
            hlog("the Bluetooth adapter is not powered: powering it on", 4)
            core.out(["bluetoothctl", "power", "on"], timeout=15)  # bounded: it waits forever for a dead bluetoothd
            shown = core.out(["bluetoothctl", "show"])
        report(state, "bluetooth-adapter", "Powered: yes" in shown, now, up=up,
               down=("Bluetooth adapter is powered off",
                     "Passkey QR needs the Bluetooth adapter on; `bluetoothctl power on` did not work."))
    elif needs_bridge and settled:
        report(state, "bluetooth-adapter", False, now, up=up,
               down=("No Bluetooth adapter",
                     "The ha-ble-vhci bridge registered no adapter, so the passkey QR dialog hangs. "
                     "See: journalctl --user -u ha-ble-vhci"))


def health_bluetooth(state: dict, now: float) -> None:
    # a bridge the user disabled on purpose (`systemctl --user disable --now`) is left alone
    needs_bridge = selfheal.unit_loaded("ha-ble-vhci") and selfheal.user_unit_enabled("ha-ble-vhci") and not bluetooth.real_adapters()
    settled = True  # a bridge that has just (re)started needs a while to register its adapter
    if needs_bridge:
        settled = _bridge_settled(state, now)
    _report_adapter(state, now, needs_bridge, settled)
    if needs_bridge:
        if state.get("pending"):  # Home Assistant is only ever asked on behalf of a request that is waiting
            health_proxy(state, now)
    else:  # a real adapter (or no bridge): an old outage must not carry over to the next time the bridge is needed
        for key in ("bluetooth-proxy", "bluetooth-adapter") if not bluetooth.bt_adapters() else ("bluetooth-proxy",):
            state.get("issues", {}).pop(key, None)


def scanner_query() -> tuple[int | None, str]:
    """Exit code of the bridge's ``--scanners`` and the last line it said on stderr (None: it cannot be asked)."""
    fake = os.environ.get("PASSKEY_QR_HEALTH_FAKE_SCANNERS_EXIT", "").strip()  # test/demo seam: pretend the exit code
    if fake.isdigit():
        return int(fake), os.environ.get("PASSKEY_QR_HEALTH_FAKE_SCANNERS_REASON", "").strip()
    result = bluetooth.scanners_result()
    if result is None:
        return None, ""
    if result.returncode == 1 and "Traceback" in (result.stderr or ""):
        return 70, ""  # the bridge itself crashed: that says nothing about Home Assistant
    lines = (result.stderr or "").strip().splitlines()
    return result.returncode, lines[-1].strip() if lines else ""


def ha_reason(detail: str) -> str:
    """What the bridge's one-line complaint means, in words for someone who is just trying to sign in."""
    low = detail.lower()
    for needles, words in (
        (("connect call failed", "connection refused"), "nothing answers on its port (it is restarting or stopped)"),
        (("name or service not known", "temporary failure in name resolution", "nodename nor servname"),
         "its host name does not resolve (network or DNS is down)"),
        (("network is unreachable", "no route to host"), "the network is unreachable"),
        (("closed the connection", "dropped the connection", "server disconnected"),
         "it dropped the connection (it is restarting)"),
        (("timed out", "timeout", "did not answer"), "no answer in time (network down or Home Assistant overloaded)"),
    ):
        if any(needle in low for needle in needles):
            return words
    return re.sub(r"^cannot reach Home Assistant at \S+: ", "", detail)[:120] or "no answer"


SAFE_SSH_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]*")
SAFE_CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def ha_repair_target() -> tuple[str, str] | None:
    """(ssh host, container name) of the user's own Home Assistant, from ~/.config/passkey-qr/ha-repair
    (HA_SSH_HOST defaults to the host of the configured Home Assistant URL). None: no repair is configured."""
    values = dict(re.findall(r"^(HA_SSH_HOST|HA_CONTAINER)=(\S+)[ \t]*$", core.read_text(HA_REPAIR_CONFIG) or "", re.M))
    host = values.get("HA_SSH_HOST") or urllib.parse.urlparse(bluetooth.ha_url_configured() or "").hostname
    container = values.get("HA_CONTAINER")
    if host and container and SAFE_SSH_HOST.fullmatch(host) and SAFE_CONTAINER.fullmatch(container):
        return host, container
    return None


def docker_time(text: str) -> float:
    """Docker's RFC 3339 timestamp (nanoseconds, Z) as epoch seconds; 0.0 when it has none ('0001-01-01...')."""
    match = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z", text)
    if not match or text.startswith("0001-"):
        return 0.0
    return calendar.timegm(time.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S")) + float("0" + (match.group(2) or ""))


def ssh_docker(host: str, *words: str, timeout: float = 25) -> subprocess.CompletedProcess | None:
    """``docker <words>`` on the user's own server over the SSH access that already works without a password
    (BatchMode: it never asks, never trusts an unknown host key). None when ssh itself cannot be run."""
    remote = "docker " + " ".join(shlex.quote(word) for word in words)
    try:
        return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, remote],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def ha_container_info(host: str, container: str) -> dict | None:
    fmt = "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|" \
          "{{.State.StartedAt}}|{{.State.FinishedAt}}"
    result = ssh_docker(host, "inspect", "--format", fmt, container)
    parts = (result.stdout.strip().split("|") if result is not None and result.returncode == 0 else [])
    if len(parts) != 4:
        return None
    return {"status": parts[0], "health": parts[1], "started": docker_time(parts[2]), "finished": docker_time(parts[3])}


def ha_container_plan(info: dict, now: float, down_for: float) -> tuple[str, str]:
    """``(action, what we found)``. Only a container that is not running, or one that stays unhealthy AND
    unreachable for minutes, is touched: a Home Assistant core restart (minutes of 'unreachable' with the
    container up and healthy or booting) is somebody's deliberate action and is never fought."""
    status, health = info["status"], info["health"]
    if status in ("exited", "dead", "created"):
        if status == "exited" and 0 <= now - info["finished"] < HA_RESTART_POLICY_GRACE_SECONDS:
            return "wait", "its container has just stopped and Docker is bringing it back"
        return "start", "its container was stopped"
    if status == "paused":
        return "unpause", "its container was paused"
    if status != "running":
        return "wait", f"its container is {status}"
    age = now - info["started"]
    if 0 <= age < HA_BOOT_GRACE_SECONDS:
        return "wait", f"its container started {age / 60:.0f} min ago and Home Assistant is still booting"
    if health == "unhealthy" and down_for >= HA_HUNG_SECONDS:
        return "restart", f"its container has been unhealthy and unreachable for {down_for / 60:.0f} min"
    if health == "unhealthy":
        return "wait", "its container is unhealthy (it is restarted if that lasts)"
    return "wait", "its container is running, so Home Assistant itself is restarting or the network is slow"


def note_ha_down(state: dict, now: float) -> float:
    """How long Home Assistant has been continuously unreachable as seen by passes of requests: observations
    further apart than HA_DOWN_GAP_SECONDS belong to different outages."""
    down = state.get("ha_down") or {}
    if not down or not 0 <= now - down.get("last", 0) <= HA_DOWN_GAP_SECONDS:
        down = {"since": now}
    down["last"] = now
    state["ha_down"] = down
    return now - down["since"]


def ha_repair(state: dict, now: float, down_for: float) -> str:
    """Home Assistant is unreachable while a request waits: look at its container over the user's own SSH access
    and start/unpause/restart it when that is what is wrong. Returns a sentence for the notification ('' when
    nothing is configured). Journal lines say exactly what was done."""
    target = ha_repair_target()
    if target is None or not may_act(state, "ha-inspect", now, HA_INSPECT_COOLDOWN):
        return ""
    host, container = target
    info = ha_container_info(host, container)
    if info is None:
        hlog(f"cannot look at {container} on {host} over ssh (no passwordless access?): not repairing", 4)
        return f"Could not look at it on {host} over SSH. "
    action, found = ha_container_plan(info, now, down_for)
    said = found[0].upper() + found[1:]
    if action != "wait" and not may_act(state, "ha-repair", now, HA_REPAIR_COOLDOWN):
        hlog(f"{container} on {host}: {found}; it was repaired less than {HA_REPAIR_COOLDOWN // 60} min ago: waiting", 4)
        return f"{said}. "
    if action == "wait":
        hlog(f"{container} on {host}: {found}: leaving it alone")
        return f"{said}. "
    hlog(f"{container} on {host}: {found}: running `docker {action}` there", 4)
    result = None if core.DRY_RUN else ssh_docker(host, action, container, timeout=HA_ACTION_TIMEOUT_SECONDS)
    if not core.DRY_RUN and (result is None or result.returncode != 0):
        hlog(f"`docker {action} {container}` on {host} failed: {(result.stderr if result else '').strip()[:200]}", 4)
        return f"{said}; repairing it did not work (see the journal). "
    return f"{said}: it was {ACTION_DONE[action]}. "


def _proxy_online(state: dict, now: float, up: tuple[str, str]) -> None:
    report(state, "bluetooth-proxy", True, now, up=up)
    if scanner_log_says_none(now) and may_act(state, "bridge-stale-scanner", now, BRIDGE_RESTART_COOLDOWN):
        hlog("Home Assistant sees a scanner but the bridge says 'none' for a while: restarting ha-ble-vhci", 4)
        restart_unit("ha-ble-vhci")


def _proxy_down_text(state: dict, now: float, code: int, detail: str) -> tuple[str, str]:
    if code == EXIT_NO_SCANNER:
        return ("Bluetooth proxy (ESP32) is offline",
                "Home Assistant has no Bluetooth proxy online, so the passkey QR dialog cannot see your phone. "
                "If Home Assistant has just restarted the ESP32 is back within a minute or two; otherwise power "
                "it on or reconnect it. You are told when it is back.")
    if code == core.EXIT_UNREACHABLE:
        seen = ha_repair(state, now, note_ha_down(state, now))
        return ("Home Assistant is unreachable",
                f"The passkey QR dialog cannot see your phone until Home Assistant ({bluetooth.ha_url_configured() or 'URL not set'}) "
                f"answers: {ha_reason(detail)}. {seen}It repairs itself; you are told when it is back.")
    return ("Home Assistant refuses the passkey bridge",
            f"{detail or 'it answered with an error'}. Check the admin token in ~/.config/ha-ble-vhci/token.")


def health_proxy(state: dict, now: float) -> None:
    up = ("Bluetooth proxy is back online", "Passkey QR works again: Home Assistant sees the ESP32.")
    code, detail = scanner_query()
    if code == core.EXIT_UNREACHABLE and not ha_reason(detail).startswith("no answer in time"):
        time.sleep(PROBE_RETRY_SECONDS)  # a blip is not an outage: look once more before the user is told
        code, detail = scanner_query()  # (a query that already waited for its timeout is not asked a second time)
    if code is None or code not in (0, 1, EXIT_NO_SCANNER, core.EXIT_UNREACHABLE):
        # Home Assistant is not configured, the bridge did not answer in time or is too old for --scanners
        # (argparse exit 2): nothing trustworthy to compare against, and not Home Assistant's fault
        if code is not None:
            hlog(f"ha-ble-vhci --scanners exited {code}: cannot tell whether a proxy is online", 4)
        return
    if code != core.EXIT_UNREACHABLE:
        state.pop("ha_down", None)  # Home Assistant answered
    if code == 0:
        _proxy_online(state, now, up)
        return
    down = _proxy_down_text(state, now, code, detail)
    report(state, "bluetooth-proxy", False, now, down=down, up=up)


def health_portal(state: dict, now: float) -> None:
    ready = credentialsd.portal_credential_ready()
    tries = state.setdefault("portal_restarts", 0)
    if ready:
        state["portal_restarts"] = 0
    elif tries < MAX_PORTAL_RESTARTS and may_act(state, "restart-portal", now, RESTART_COOLDOWN):
        # other apps use the portal too: after a few restarts that did not help, only report
        state["portal_restarts"] = tries + 1
        hlog("the portal does not offer org.freedesktop.portal.experimental.Credential: restarting xdg-desktop-portal", 4)
        restart_unit("xdg-desktop-portal")
        deadline = time.monotonic() + 20  # a hung portal makes every probe slow: bound the wait by time, not tries
        while time.monotonic() < deadline:
            ready = credentialsd.portal_credential_ready()
            if ready:
                break
            time.sleep(1)
    report(state, "portal", ready, now,
           down=("The credential portal is not working",
                 "Passkey sign-in cannot start: xdg-desktop-portal has no Credential interface and a restart "
                 "did not fix it. See: journalctl --user -u xdg-desktop-portal"),
           up=("The credential portal works again", "Passkey sign-in can start again."))


def start_activatable(name: str) -> bool:
    return core.out(["busctl", "--user", "call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
                     "org.freedesktop.DBus", "StartServiceByName", "su", name, "0"], timeout=30).startswith("u ")


def health_credentialsd(state: dict, now: float) -> None:
    for unit in selfheal.loaded_units(CREDENTIALSD_UNITS):
        started = start_activatable(unit)
        if not started and may_act(state, f"restart-{unit}", now, RESTART_COOLDOWN):
            hlog(f"{unit} did not start on demand: restarting it", 4)
            restart_unit(unit)
            started = start_activatable(unit)
        report(state, unit, started, now,
               down=(f"{unit} does not start", f"Passkey sign-in cannot work. See: journalctl --user -u {unit}"),
               up=(f"{unit} works again", "Passkey sign-in can start again."))


def stuck_helpers(limit: float) -> list[tuple[int, int]]:
    """``(pid, age in seconds)`` of this user's Firefox native-messaging helpers older than ``limit``."""
    found = []
    for line in core.out(["ps", "-u", str(os.getuid()), "-o", "pid=,etimes=,args="]).splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not (parts[0].isdigit() and parts[1].isdigit()) or int(parts[0]) == os.getpid():
            continue
        argv = parts[2].split()  # the helper is a script: `python3 .../credentialsd-firefox-helper`, or run directly
        if Path(argv[0]).name == "env" and len(argv) > 1:
            argv = argv[1:]
        script = argv[1] if Path(argv[0]).name.startswith("python") and len(argv) > 1 else argv[0]
        if Path(script).name == HELPER_NAME and int(parts[1]) >= limit:
            found.append((int(parts[0]), int(parts[1])))
    return found


def still_helper(pid: int) -> bool:
    """The pid is still the helper `ps` showed (it may have exited and the pid been reused since)."""
    try:
        return HELPER_NAME in Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except OSError:
        return False


def stuck_limit() -> float:
    try:
        return float(os.environ.get("PASSKEY_QR_STUCK_SECONDS") or STUCK_SECONDS)  # the seam used by demos
    except ValueError:
        return STUCK_SECONDS


def _kill_stuck_helpers(stuck: list[tuple[int, int]]) -> None:
    for pid, age in stuck:
        hlog(f"stuck passkey request: {HELPER_NAME} pid {pid} is {age}s old: sending it SIGKILL", 4)
        if not core.DRY_RUN and still_helper(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def health_stuck_requests(state: dict, now: float) -> None:
    """The helper is spawned once per WebAuthn request (its own timeout defaults to 5 min) and ignores SIGTERM
    when its request hangs; an abandoned request keeps credentialsd scanning forever, so end it and restart
    credentialsd + its UI - but only once no other request is in flight (the restart would abort it)."""
    helpers = stuck_helpers(0)
    limit = stuck_limit()
    stuck = [(pid, age) for pid, age in helpers if age >= limit]
    _kill_stuck_helpers(stuck)
    if stuck:
        state["restart_credentialsd"] = True
    if not state.get("restart_credentialsd"):
        return
    if any(pid not in {p for p, _ in stuck} for pid, _ in helpers):
        hlog("another passkey request is in progress: restarting credentialsd after it ends")
        return
    restart_credentialsd(state)


def restart_credentialsd(state: dict) -> None:
    for unit in selfheal.loaded_units(CREDENTIALSD_UNITS):
        core.run(["systemctl", "--user", "restart", unit], check=False)
    state["restart_credentialsd"] = False


def health(now: float | None = None) -> int:
    """One pass over the chain for a passkey request that is pending right now (see ``serve_request``): the only
    place that asks Home Assistant and the only one whose findings may reach the user."""
    now = time.time() if now is None else now
    state = load_health_state()
    state["pending"] = True
    graphical = session_ready()  # the portal and credentialsd only live inside the graphical session
    if not graphical:
        hlog("no graphical session (no graphical-session.target, no display in the user manager): the portal and "
             "credentialsd checks are skipped")
    # the stuck-request cleanup comes first: it is the cheapest check and the one a hung portal must not starve;
    # the bluetooth check goes last because it is the only one that can wait on the network (Home Assistant)
    checks = [health_stuck_requests, *([health_portal, health_credentialsd] if graphical else []), health_bluetooth]
    failed = False
    for check in checks:
        try:
            check(state, now)
        except Exception:  # one broken check must not stop the others
            hlog(f"{check.__name__} crashed:\n{traceback.format_exc()}", 3)
            failed = True
        save_health_state(state)  # after every check: a run cut off by a stop keeps what it learned
    return 1 if failed else 0


def pending_helpers() -> list[tuple[int, int]]:
    """``(pid, age)`` of the Firefox native-messaging helpers alive right now: one per passkey request that is
    still waiting for its answer (it exits when the request ends)."""
    return stuck_helpers(0)


def mark_request(pending: bool) -> None:
    """The marker file other tools (and ``status``) can look at: it exists exactly while a request is served."""
    if core.DRY_RUN:
        return
    try:
        if pending:
            REQUEST_MARKER.parent.mkdir(parents=True, exist_ok=True)
            REQUEST_MARKER.write_text(json.dumps({"since": time.time(), "pid": os.getpid()}))
        else:
            REQUEST_MARKER.unlink(missing_ok=True)
    except OSError as err:
        hlog(f"cannot update {REQUEST_MARKER}: {err}", 4)


def end_request() -> None:
    """The request is over: what was told during it is settled, so a later request starts from scratch, and a
    credentialsd restart that waited for the other requests (see health_stuck_requests) is done now."""
    state = load_health_state()
    state["pending"] = False
    for entry in state.get("issues", {}).values():
        entry["open"], entry["fails"] = False, 0
    state.pop("ha_down", None)
    if state.get("restart_credentialsd") and not pending_helpers():
        hlog("the last passkey request ended: restarting credentialsd after the stuck one")
        restart_credentialsd(state)
    save_health_state(state)


def serve_request(sleep: Callable[[float], None] = time.sleep) -> None:
    """A passkey request is pending: check the chain now and every REQUEST_CHECK_SECONDS for as long as a Firefox
    helper still waits for its answer. A pass is only started while the request is still alive, so one that
    ended during the wait costs nothing and tells nobody."""
    hlog("passkey request pending: checking the chain")
    mark_request(True)
    try:
        with pass_lock(wait=True):
            health()
        while pending_helpers():
            sleep(REQUEST_CHECK_SECONDS)
            if pending_helpers():
                with pass_lock(wait=True):
                    health()
    finally:
        mark_request(False)
        with pass_lock(wait=True):
            end_request()
    hlog("passkey request over: nothing is checked until the next one")


@contextlib.contextmanager
def pass_lock(wait: bool) -> Iterator[bool]:
    """Health passes (the watcher's and a manual `health`) share cooldowns and one state file: one at a time.
    Yields True when held, False when ``wait`` is off and another pass holds it."""
    try:
        PASS_LOCK.parent.mkdir(parents=True, exist_ok=True)
        handle = open(PASS_LOCK, "a")
    except OSError as err:  # no runtime directory: a pass without the lock beats no pass
        hlog(f"cannot open {PASS_LOCK}: {err}", 4)
        yield True
        return
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        handle.close()  # releases the lock


def request_call_time(line: str) -> float | None:
    """Epoch seconds of a ``busctl monitor --json`` line that may start a passkey request: a call to the portal that
    creates or gets a credential, or the earlier registration of Firefox's app id that the helper makes first
    (a hung portal never gets as far as the credential call). The caller still checks that a helper is alive."""
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(message, dict) or message.get("type") != "method_call" or message.get("destination") != PORTAL_NAME:
        return None
    interface, member = message.get("interface"), message.get("member")
    data = (message.get("payload") or {}).get("data") if isinstance(message.get("payload"), dict) else None
    firefox_registers = (interface == REGISTRY_INTERFACE and member == "Register"
                         and isinstance(data, list) and data[:1] == [core.FIREFOX_APP_ID])
    if not (firefox_registers or (interface == REQUEST_INTERFACE and member in REQUEST_MEMBERS)):
        return None
    stamp = message.get("timestamp-realtime")
    return stamp / 1e6 if isinstance(stamp, (int, float)) else time.time()


def cmd_watch(_: argparse.Namespace) -> None:
    """Run by passkey-qr-watch.service. Blocks on the session bus, so it costs nothing while idle: no timer, no
    Home Assistant query, no notification. A portal call that comes from Firefox's helper starts
    ``serve_request``; the monitor is attached before anything else so no request can slip in between."""
    mark_request(False)  # a marker left behind by a killed run
    monitor = subprocess.Popen(["busctl", "--user", "monitor", "--json=short",
                                *[arg for rule in REQUEST_MATCHES for arg in ("--match", rule)]],
                               stdout=subprocess.PIPE, text=True)
    hlog("watching the session bus for passkey requests: nothing is checked until one arrives")
    handled_until = 0.0
    if pending_helpers():  # a request was already waiting when the watcher (re)started
        serve_request()
        handled_until = time.time()
    assert monitor.stdout
    for line in monitor.stdout:
        asked = request_call_time(line)
        if asked is None or asked < handled_until:  # not a request, or one made while the last one was served
            continue
        time.sleep(REQUEST_SETTLE_SECONDS)  # whatever called the portal must have a Firefox helper behind it
        if pending_helpers():
            serve_request()
        else:
            hlog("a portal call without a Firefox helper behind it: not a passkey request")
        handled_until = time.time()
    sys.exit(f"busctl monitor ended (exit {monitor.wait()}): systemd restarts the watcher")


def cmd_health(_: argparse.Namespace) -> None:
    """One pass now, but only for a pending request: with none, nothing is queried and nobody is told. The
    watcher serving the request already does this by itself, so it is not duplicated. Exit 1 only when a check
    itself crashed."""
    if not pending_helpers():
        hlog("no passkey request is pending: nothing to check")
        return
    if REQUEST_MARKER.exists():
        hlog("the watcher is serving this request already: nothing to do")
        return
    with pass_lock(wait=False) as held:
        if not held:
            hlog("another health pass is running: nothing to do")
            return
        failed = health()
        end_request()
    sys.exit(failed)


def show_request_status() -> None:
    try:
        since = json.loads(REQUEST_MARKER.read_text()).get("since")
    except (OSError, json.JSONDecodeError, AttributeError):
        core.say("\n  passkey request: none pending (nothing is being checked)")
        return
    core.say(f"\n  passkey request: pending since {time.strftime('%H:%M:%S', time.localtime(since))}"
             if isinstance(since, (int, float)) else "\n  passkey request: pending")


def cmd_heal_prefs(_: argparse.Namespace) -> None:
    """Run by passkey-qr-prefs.path and at login: re-apply only the managed user.js block when it drifted.
    Journal only: nobody is using a passkey right now, so nobody is notified."""
    if not extension.managed_profiles():
        hlog("no Firefox profile to manage yet")
        return
    if extension.usb_token_off_ok():
        hlog("Firefox prefs are in place")
        return
    hlog("Firefox prefs drifted (a Firefox update or a new profile dropped the user.js block): re-applying; "
         "they take effect at the next Firefox start", 4)
    extension.disable_usb_token()
