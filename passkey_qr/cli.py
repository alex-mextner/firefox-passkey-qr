"""passkey-qr: make Firefox on Ubuntu offer "use your phone (QR)" for passkeys.

Installs and wires together, step by step and only after you confirm each step:
  * Firefox from Mozilla's apt repo (the snap sandbox blocks native messaging)
  * ha-ble-vhci, if the PC has no Bluetooth but Home Assistant with an ESPHome
    Bluetooth proxy is reachable (hybrid/QR needs the PC to hear the phone's BLE)
  * credentialsd + patched xdg-desktop-portal (built from source; missing build
    dependencies are found and installed automatically)
  * the signed credentialsd Firefox extension and its native-messaging host
  * self-healing: systemd restart policy for every service of the chain, a watcher that wakes only when a passkey
    request starts (no timer, no polling), repairs hung pieces and tells you once on the desktop if Home
    Assistant or the Bluetooth proxy is down, and re-applies the Firefox prefs

Every action has a check that looks at the real system state, so re-running
skips whatever is already applied and `passkey-qr status` shows what is left.

Touch ID is not supported: the Magic Keyboard sensor only works with a Mac's
Secure Enclave.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import traceback

from passkey_qr import (
    apt,
    bluetooth,
    core,
    credentialsd,
    extension,
    firefox,
    health,
    selfheal,
)


def packages_actions() -> list[core.Action]:
    return [core.Action("all required apt packages installed", lambda: not apt.missing_apt(), lambda: apt.apt_install(apt.missing_apt()))]


def step_packages() -> bool:
    return core.run_actions("1. Build and runtime packages", packages_actions())


def step_firefox() -> bool:
    return core.run_actions("2. Firefox (non-snap)", firefox.firefox_actions(),
                            "  The snap sandbox blocks the native-messaging host the extension needs.")


def step_bluetooth() -> bool:
    if bluetooth.real_adapters():
        core.say("\n== 3. Bluetooth for the QR (hybrid) flow ==")
        core.say("  local adapter(s) present: " + ", ".join(bluetooth.real_adapters()) + " - nothing to do")
        return True
    return core.run_actions(
        "3. Bluetooth for the QR (hybrid) flow", bluetooth.bluetooth_actions(),
        "  No physical Bluetooth adapter. The QR flow needs the PC to hear the phone's BLE advert;\n"
        "  ha-ble-vhci provides that from a Home Assistant Bluetooth proxy.")


def step_credentialsd() -> bool:
    return core.run_actions("4. credentialsd + patched xdg-desktop-portal", credentialsd.credentialsd_actions(),
                            "  Built from source into /usr/local; this takes a while.")


def step_extension() -> bool:
    return core.run_actions("5. Firefox extension + native-messaging host", extension.extension_actions())


def step_selfheal() -> bool:
    return core.run_actions("6. Self-healing (restart policy, request watcher, Firefox prefs)", selfheal.selfheal_actions(),
                            "  Every link of the chain restarts on its own. While you do NOT use passkeys nothing runs and\n"
                            "  nothing is checked; when a passkey request starts, a watcher repairs what is hung and tells\n"
                            "  you on the desktop (once) if Home Assistant or the Bluetooth proxy (ESP32) is down.")


STEPS = [step_packages, step_firefox, step_bluetooth, step_credentialsd, step_extension, step_selfheal]


def status_groups() -> list[tuple[str, list[core.Action]]]:
    """The step groups with their actions; step 3 has none when a physical adapter is present."""
    return [
        ("1. Packages", packages_actions()),
        ("2. Firefox", firefox.firefox_actions()),
        ("3. Bluetooth via Home Assistant", [] if bluetooth.real_adapters() else bluetooth.bluetooth_actions()),
        ("4. credentialsd + portal", credentialsd.credentialsd_actions()),
        ("5. Extension", extension.extension_actions()),
        ("6. Self-healing", selfheal.selfheal_actions()),
    ]


def cmd_status(_: argparse.Namespace) -> None:
    for title, actions in status_groups():
        if title.startswith("3.") and bluetooth.real_adapters():
            core.say("\n== 3. Bluetooth ==\n  local adapter(s): " + ", ".join(bluetooth.real_adapters()))
            continue
        core.say(f"\n== {title} ==")
        core.show_actions(actions)
        if title.startswith("3."):
            bluetooth.show_proxy_status()
    missing = apt.missing_apt()
    if missing:
        core.say("\n  missing packages: " + " ".join(missing))
    health.show_request_status()


def status_rows() -> list[tuple[str, bool, bool]]:
    """``(label, ok, required)``: ``required`` rows decide ``check``; the others are context.
    Bluetooth is one required row (any adapter, local or ha-ble-vhci); how it is provided is context."""
    rows = [("Bluetooth adapter (local or ha-ble-vhci)", bool(bluetooth.bt_adapters()), True)]
    for title, actions in status_groups():
        if title.startswith("3."):
            rows += [(a.name, a.check(), False) for a in actions]
        else:
            rows += [(a.name, a.check(), a.required and not a.optional) for a in actions]
    return rows


def unmet_rows(rows: list[tuple[str, bool, bool]]) -> list[str]:
    return [label for label, ok, required in rows if required and not ok]


def cmd_check(_: argparse.Namespace) -> None:
    """Read-only: exit 0 when every required row holds, 3 (and the unmet rows) otherwise."""
    unmet = unmet_rows(status_rows())
    for label in unmet:
        core.say(f"  missing: {label}")
    if unmet and (conflict := firefox.mozilla_conflict()):
        core.say(f"  {conflict}")
    sys.exit(3 if unmet else 0)


SNAPSHOT_TIMEOUT = 8  # seconds per diagnostic command: the snapshot must never delay the real error message


def log_snapshot() -> None:
    """Write the facts needed to debug a run into the log file (no secrets); never raises."""
    if not core.LOG_FILE:
        return
    try:
        _write_snapshot()
    except Exception as err:  # a failing diagnostic must not mask the StepError that called it
        core.log(f"(snapshot failed: {err})")


def _write_snapshot() -> None:
    cmds = [
        ["uname", "-a"], [core.FIREFOX_BIN, "--version"], ["bluetoothctl", "list"],
        ["systemctl", "--user", "is-active", "ha-ble-vhci", "xdg-desktop-portal", "dbus"],
        ["busctl", "--user", "list", "--activatable", "--no-pager"],
        ["busctl", "--user", "introspect", "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop", "--no-pager"],
        ["cat", str(core.FIREFOX_POLICIES)], ["ls", "-l", str(core.HOME / ".mozilla/native-messaging-hosts")],
        ["journalctl", "--user", "-u", "ha-ble-vhci", "-n", "25", "--no-pager"],
        ["journalctl", "--user", "-u", "xdg-desktop-portal", "-n", "25", "--no-pager"],
        ["journalctl", "--user", "-n", "40", "--no-pager", "-t", "credentialsd"],
        ["journalctl", "--user", "-n", "25", "--no-pager", "-u", "xyz.iinuwa.credentialsd*"],
    ]
    core.log("\n===== snapshot =====")
    for cmd in cmds:
        core.log(f"$ {' '.join(cmd)}")
        core.log(core.out(cmd, timeout=SNAPSHOT_TIMEOUT) or "(empty)")
    core.log(f"active profile: {firefox.active_profile()} (installs.ini default: {firefox.install_default_name()})")
    for prefs in core.MOZ_PROFILE.glob("*/extension-preferences.json"):
        try:
            core.log(f"{prefs}: {json.loads(prefs.read_text()).get(core.XPI_ID)}")
        except (OSError, json.JSONDecodeError):
            pass
    core.log(f"extension_in_profile={extension.extension_in_profile()} granted={extension.extension_granted()}")
    core.log("===== end snapshot =====")


def open_test_page() -> None:
    if core.ASSUME_YES:  # unattended runs (ubuntu-tweak init) must not pop a browser tab open
        return
    url = "https://webauthn.io"
    if not core.confirm(f"Open {url} in Firefox now to test the phone/QR option?"):
        return
    if core.DRY_RUN:
        core.run([core.FIREFOX_BIN, "--new-tab", url])
        return
    subprocess.Popen([core.FIREFOX_BIN, "--new-tab", url], start_new_session=True, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)


def cmd_install(_: argparse.Namespace) -> None:
    """Exit codes: 0 everything required is in place; 3 finished what it could and something waits for
    you (listed); 1 a step failed (StepError / failed command, see main)."""
    core.say("passkey-qr changes your system step by step; "
             + ("--yes answers every step for you" if core.ASSUME_YES else "every step asks first")
             + ", and anything already\napplied (checked against the real system state) is skipped.")
    for step in STEPS:
        step()
    if core.DRY_RUN:
        if core.NOTES:
            core.say("\nWould need you: " + "; ".join(core.NOTES))
        return
    rows = status_rows()  # every check runs once; the printout, exit code and hints all come from it
    core.say("\nFinal state:")
    for label, ok, _ in rows:
        core.say(f"  [{'x' if ok else ' '}] {label}")
    log_snapshot()
    unmet = unmet_rows(rows)
    if core.NOTES or unmet:
        _report_unfinished(unmet)
        sys.exit(3)
    _say_final_advice(rows)
    open_test_page()


def _report_unfinished(unmet: list[str]) -> None:
    core.say("\nNot finished:")
    for note in core.NOTES:
        core.say(f"  waiting for you: {note}")
    for label in unmet:
        core.say(f"  missing: {label}")
    core.say("Do that, then re-run `passkey-qr install`: finished actions are skipped.")


def _say_final_advice(rows: list[tuple[str, bool, str]]) -> None:
    if all(ok for label, ok, _ in rows if label.startswith("Firefox restarted")):
        core.say("\nEverything is installed and verified. On webauthn.io choose register/sign in, then the "
                 "phone/QR option and scan the code (the phone must be within BLE range of the ESP32 proxy).")
    else:
        core.say("\nEverything is installed. Restart Firefox once: it then installs the extension and offers "
                 "the phone/QR option (test on https://webauthn.io; the phone must be within BLE range "
                 "of the ESP32 proxy).")


def main() -> None:
    parser = argparse.ArgumentParser(prog="passkey-qr", description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the commands, change nothing, auto-answer yes")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="answer yes to every step and never prompt (a token must be in $HA_TOKEN or the "
                             "token file; the Firefox snap is kept unless --remove-snap)")
    parser.add_argument("--remove-snap", action="store_true",
                        help="with --yes, also remove the Firefox snap after the deb is installed")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show which actions are really applied").set_defaults(func=cmd_status)
    sub.add_parser("check", help="exit 0 when everything required is in place, 3 otherwise (read-only)"
                   ).set_defaults(func=cmd_check)
    sub.add_parser("install", help="apply everything that is missing").set_defaults(func=cmd_install)
    sub.add_parser("watch", help="block on the session bus and check/repair the passkey chain only while a passkey "
                   "request is pending (run by passkey-qr-watch.service; no timer, no polling)"
                   ).set_defaults(func=health.cmd_watch)
    sub.add_parser("health", help="one check/repair pass now, only if a passkey request is pending (else it does nothing)"
                   ).set_defaults(func=health.cmd_health)
    sub.add_parser("heal-prefs", help="re-apply the Firefox user.js block if it drifted (run by passkey-qr-prefs.path)"
                   ).set_defaults(func=health.cmd_heal_prefs)
    args = parser.parse_args()
    core.DRY_RUN, core.ASSUME_YES, core.REMOVE_SNAP = args.dry_run, args.yes, args.remove_snap
    log_path = core.init_log(args.cmd) if args.cmd == "install" and not core.DRY_RUN else None
    if log_path:
        core.say(f"log file: {log_path}")
    where = f"Full log: {log_path}" if log_path else "(no log file: not an install run)"

    def waiting() -> str:
        """What already waits for a person, so a later failure does not hide it."""
        return "".join(f"\nstill waiting for you: {note}" for note in core.NOTES)

    try:
        args.func(args)
    except core.StepError as err:
        core.log(f"STOPPED: {err}")
        print(f"\nstopped: {err}\nFix the cause and re-run `passkey-qr install`: finished actions are skipped.\n"
              f"{where}{waiting()}", file=sys.stderr, flush=True)
        log_snapshot()  # after the message: a broken session must not delay it
        sys.exit(1)
    except subprocess.CalledProcessError as err:
        core.log(f"COMMAND FAILED (exit {err.returncode}): {err.cmd}")
        print(f"\ncommand failed (exit {err.returncode}): {' '.join(map(str, err.cmd))}\n"
              f"Re-run `passkey-qr install` after fixing it: finished actions are skipped.\n{where}{waiting()}",
              file=sys.stderr, flush=True)
        log_snapshot()
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        core.log("aborted by user")
        sys.exit("\naborted")
    except Exception:
        core.log(traceback.format_exc())
        raise
    if log_path:
        core.say(f"\nlog file: {log_path}")
