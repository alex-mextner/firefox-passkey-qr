"""Step 6: the systemd restart policy, the installed copy of this program and the watcher/prefs units."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

from passkey_qr import core

# Every link of the passkey/QR chain (bridge, portal, credentialsd, its UI, the Firefox helper, the Firefox
# prefs) restarts and repairs itself: systemd drop-ins restart a dead service at once; `watch` (a unit blocked
# on the session bus: no timer, no polling) wakes when a passkey request starts and only then checks the chain,
# repairs what systemd cannot see (hung, missing D-Bus interface, stuck request, stopped Home Assistant
# container) and tells the user, once, if Home Assistant or the ESP32 proxy is down; `heal-prefs` (run by a path
# unit) puts the Firefox prefs back. While nobody uses a passkey nothing is queried and nobody is notified.
ENTRY = "passkey-qr"  # the executable at the root of a checkout, and of the installed copy
PACKAGE = "passkey_qr"  # the directory next to it that holds every module
LIBEXEC_DIR = core.HOME / ".local/libexec/passkey-qr"
HEALTH_BIN = LIBEXEC_DIR / ENTRY  # the entry script of a copy of this program: the units survive a moved or switched checkout
SYSTEMD_USER = core.HOME / ".config/systemd/user"
SOURCE_ROOT = Path(__file__).resolve().parent.parent  # the checkout (or the copy itself) this process runs from

RESTART_SEC = {  # unit -> RestartSec; a unit that is not installed on this machine is skipped
    "ha-ble-vhci": 5,
    "xdg-desktop-portal": 2,
    "xyz.iinuwa.credentialsd.Credentials": 2,
    "xyz.iinuwa.credentialsd.UiControl": 2,
    "passwords-appd": 5,
}

OBSOLETE_UNITS = ("passkey-qr-health.timer", "passkey-qr-health.service")  # the 45 s poll the watcher replaced


def unit_properties(units: Sequence[str]) -> dict[str, dict[str, str]]:
    """LoadState and Restart of several user units from ONE systemctl call, keyed by unit name (no .service)."""
    text = core.out(["systemctl", "--user", "show", "-p", "Id", "-p", "LoadState", "-p", "Restart",
                     *[f"{unit}.service" for unit in units]])
    found = {}
    for block in text.split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if props.get("Id", "").endswith(".service"):
            found[props["Id"][:-len(".service")]] = props
    return found


def loaded_units(units: Sequence[str]) -> list[str]:
    props = unit_properties(units)
    return [unit for unit in units if props.get(unit, {}).get("LoadState") == "loaded"]


def unit_loaded(unit: str) -> bool:
    return bool(loaded_units([unit]))


def restart_dropin(unit: str) -> Path:
    return SYSTEMD_USER / f"{unit}.service.d/99-passkey-qr-selfheal.conf"  # sorts last: a later Restart= must not win


def restart_dropin_text(unit: str) -> str:
    return (
        "# Managed by `passkey-qr install`: restart this service whenever it dies, without a start limit.\n"
        "# A RestartPreventExitStatus= set in another drop-in still applies (a deliberate 'do not restart').\n"
        f"[Unit]\nStartLimitIntervalSec=0\n\n[Service]\nRestart=always\nRestartSec={RESTART_SEC[unit]}\n"
    )


def restart_policy_ok() -> bool:
    props = unit_properties(list(RESTART_SEC))
    if not props:  # the user manager could not be asked: that is not "nothing to do"
        return False
    return all(props[unit].get("Restart") == "always" and core.read_text(restart_dropin(unit)) == restart_dropin_text(unit)
               for unit in RESTART_SEC if props.get(unit, {}).get("LoadState") == "loaded")


def write_restart_policy() -> None:
    props = unit_properties(list(RESTART_SEC))
    if not props and not core.DRY_RUN:
        raise core.StepError("cannot ask the systemd user manager (no user session bus?): run this from your login session")
    for unit in RESTART_SEC:
        if props.get(unit, {}).get("LoadState") == "loaded":
            core.write_file(restart_dropin(unit), restart_dropin_text(unit))
    core.run(["systemctl", "--user", "daemon-reload"])


def program_files(root: Path) -> dict[str, str] | None:
    """The program under `root`: the entry script and every module of the package (relative path -> text)."""
    files = {}
    for path in [root / ENTRY, *sorted((root / PACKAGE).glob("*.py"))]:
        text = core.read_text(path)
        if text is None:
            return None
        files[str(path.relative_to(root))] = text
    return files


def health_copy_ok() -> bool:
    mine = program_files(SOURCE_ROOT)
    return mine is not None and program_files(HEALTH_BIN.parent) == mine and os.access(HEALTH_BIN, os.X_OK)


def install_health_copy() -> None:
    mine = program_files(SOURCE_ROOT)
    if mine is None:
        raise core.StepError(f"cannot read {SOURCE_ROOT} to install the health check")
    target = HEALTH_BIN.parent
    core.say(f"  write {HEALTH_BIN} (mode 755) and {target / PACKAGE}/*.py")
    if core.DRY_RUN:
        return
    (target / PACKAGE).mkdir(parents=True, exist_ok=True)
    for relative, text in sorted(mine.items(), key=lambda item: item[0] == ENTRY):  # the entry script goes last
        path = target / relative
        tmp = path.with_suffix(".tmp")  # the watcher may be running the old copy right now: replace, never truncate
        tmp.write_text(text)
        tmp.chmod(0o755 if relative == ENTRY else 0o644)
        tmp.replace(path)
    for stale in (target / PACKAGE).glob("*.py"):  # a module this version no longer has
        if str(stale.relative_to(target)) not in mine:
            stale.unlink()
    if (SYSTEMD_USER / "passkey-qr-watch.service").exists():  # a watcher that runs the old copy picks up the new one
        core.run(["systemctl", "--user", "try-restart", "passkey-qr-watch.service"], check=False)


def unit_path(path: Path) -> str:
    """A path as systemd reads it in a unit file: ``%`` is a specifier character."""
    return str(path).replace("%", "%%")


def watch_unit_files() -> dict[Path, str]:
    return {
        SYSTEMD_USER / "passkey-qr-watch.service": (
            "[Unit]\nDescription=passkey-qr: check and repair the passkey chain while a passkey request is pending "
            "(blocked on the session bus, no polling)\nStartLimitIntervalSec=0\n\n"
            f"[Service]\nExecStart=\"{unit_path(HEALTH_BIN)}\" watch\nSyslogIdentifier=passkey-qr-watch\n"
            "Restart=always\nRestartSec=5\nNice=10\n\n[Install]\nWantedBy=default.target\n"),
    }


def prefs_unit_files() -> dict[Path, str]:
    return {
        SYSTEMD_USER / "passkey-qr-prefs.service": (
            "[Unit]\nDescription=Re-apply the passkey-qr Firefox prefs (user.js managed block) when they drifted\n\n"
            f"[Service]\nType=oneshot\nExecStart=\"{unit_path(HEALTH_BIN)}\" heal-prefs\nSyslogIdentifier=passkey-qr-prefs\n\n"
            "[Install]\nWantedBy=default.target\n"),  # also runs once per login
        SYSTEMD_USER / "passkey-qr-prefs.path": (
            "[Unit]\nDescription=Watch the Firefox profile list (a Firefox update or a new profile can drop the prefs)\n\n"
            f"[Path]\nPathModified={unit_path(core.MOZ_PROFILE / 'profiles.ini')}\nPathModified={unit_path(core.MOZ_PROFILE / 'installs.ini')}\n"
            "Unit=passkey-qr-prefs.service\n\n[Install]\nWantedBy=default.target\n"),
    }


def unit_files_current(files: dict[Path, str]) -> bool:
    return all(core.read_text(path) == text for path, text in files.items())


def user_unit_enabled(unit: str) -> bool:
    return core.out(["systemctl", "--user", "is-enabled", unit]) == "enabled"


def watch_ok() -> bool:
    return (unit_files_current(watch_unit_files()) and os.access(HEALTH_BIN, os.X_OK)
            and user_unit_enabled("passkey-qr-watch.service") and core.service_active("passkey-qr-watch.service", user=True))


def enable_watch() -> None:
    for path, text in watch_unit_files().items():
        core.write_file(path, text)
    core.run(["systemctl", "--user", "daemon-reload"])
    core.run(["systemctl", "--user", "enable", "passkey-qr-watch.service"])
    core.run(["systemctl", "--user", "restart", "passkey-qr-watch.service"])  # a running one keeps the old code


def obsolete_units_gone() -> bool:
    return not any((SYSTEMD_USER / name).exists() for name in OBSOLETE_UNITS)


def remove_obsolete_units() -> None:
    core.run(["systemctl", "--user", "disable", "--now", "passkey-qr-health.timer"], check=False)
    for name in OBSOLETE_UNITS:
        core.say(f"  remove {SYSTEMD_USER / name}")
        if not core.DRY_RUN:
            (SYSTEMD_USER / name).unlink(missing_ok=True)
    core.run(["systemctl", "--user", "daemon-reload"])


def prefs_path_ok() -> bool:
    return (unit_files_current(prefs_unit_files()) and user_unit_enabled("passkey-qr-prefs.path")
            and user_unit_enabled("passkey-qr-prefs.service") and core.service_active("passkey-qr-prefs.path", user=True))


def enable_prefs_path() -> None:
    for path, text in prefs_unit_files().items():
        core.write_file(path, text)
    core.run(["systemctl", "--user", "daemon-reload"])
    core.run(["systemctl", "--user", "enable", "passkey-qr-prefs.service"])
    core.run(["systemctl", "--user", "enable", "--now", "passkey-qr-prefs.path"])


def selfheal_actions() -> list[core.Action]:
    return [
        core.Action("services of the chain restart on their own (Restart=always, no start limit)", restart_policy_ok,
                    write_restart_policy),
        core.Action(f"health check copy is current ({HEALTH_BIN})", health_copy_ok, install_health_copy,
                    required=False),  # a checkout update makes it stale: `install` refreshes it, `check` must not alarm
        core.Action("passkey-qr-watch.service repairs the chain and flags Home Assistant/ESP32 problems only while a "
                    "passkey request is pending (no timer, no polling)", watch_ok, enable_watch),
        core.Action("the old 45 s passkey-qr-health.timer is gone (it polled Home Assistant while idle)",
                    obsolete_units_gone, remove_obsolete_units),
        core.Action("Firefox prefs are re-applied after a profile change (passkey-qr-prefs.path)", prefs_path_ok,
                    enable_prefs_path),
    ]
