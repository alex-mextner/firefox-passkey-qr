"""Step 5: the credentialsd Firefox extension, its native-messaging host, the policy and the Firefox prefs."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from passkey_qr import core, credentialsd, firefox

XPI_URL = f"{core.CREDSD_REPO}/releases/download/{core.CREDSD_TAG}/credentialsd-helper@iinuwa.xyz-0.2.0.xpi"

NM_MANIFEST = "xyz.iinuwa.credentialsd_helper.json"

APP_DESKTOP = core.HOME / ".local/share/applications/org.mozilla.firefox.desktop"


def nm_link() -> Path:
    return core.HOME / ".mozilla/native-messaging-hosts" / NM_MANIFEST


def nm_target() -> Path:
    """Where meson installed the manifest (libdir is multiarch on Debian/Ubuntu: lib/x86_64-linux-gnu)."""
    for dest in credentialsd.installed_map(next(p for p in credentialsd.PROJECTS if p.name == "credentialsd")).values():
        if dest.endswith(NM_MANIFEST):
            return Path(dest)
    # lib/mozilla/... or the multiarch lib/<triplet>/mozilla/...: two fixed depths, never a walk of /usr/local/lib
    pattern = f"mozilla/native-messaging-hosts/{NM_MANIFEST}"
    found = next(iter(sorted([*Path("/usr/local").glob(f"lib*/{pattern}"), *Path("/usr/local").glob(f"lib*/*/{pattern}")])), None)
    if found:
        return found
    return Path("/usr/local/lib/mozilla/native-messaging-hosts") / NM_MANIFEST


def nm_ok() -> bool:
    link = nm_link()
    return link.is_symlink() and link.exists()


def link_nm() -> None:
    if not core.DRY_RUN:
        nm_link().parent.mkdir(parents=True, exist_ok=True)
    core.run(["ln", "-sfn", str(nm_target()), str(nm_link())])


def extension_in_profile() -> bool:
    profile = firefox.active_profile()
    if not profile:
        return False
    try:
        addons = json.loads((profile / "extensions.json").read_text()).get("addons", [])
    except (OSError, json.JSONDecodeError):
        return False
    return any(addon.get("id") == core.XPI_ID and addon.get("active") for addon in addons)


def extension_granted() -> bool:
    """The policy install grants <all_urls> automatically; confirm it in the profile."""
    profile = firefox.active_profile()
    if not profile:
        return False
    try:
        entry = json.loads((profile / "extension-preferences.json").read_text()).get(core.XPI_ID, {})
    except (OSError, json.JSONDecodeError):
        return False
    return "<all_urls>" in entry.get("origins", [])


def extension_ready() -> bool:
    return extension_in_profile() and extension_granted()


def restart_firefox() -> None:
    if core.ASSUME_YES:
        # an unattended run (ubuntu-tweak init) neither closes the user's browser nor opens one over ssh:
        # the policy is in place, so the extension is installed at Firefox's next start (also in --dry-run)
        core.say("  Firefox is not restarted by --yes: the extension is installed by policy at its next start")
        return
    if firefox.firefox_running():
        core.say("  closing Firefox (session restore brings your tabs back)")
        core.run(["pkill", "-TERM", "-x", "firefox"], check=False)
        if core.DRY_RUN:  # nothing was closed: do not wait for it, show the launch and stop
            core.run([core.FIREFOX_BIN])
            return
        for _ in range(40):
            if not firefox.firefox_running():
                break
            time.sleep(1)
        else:
            raise core.StepError("Firefox did not close within 40 s: close it manually and re-run.")
    fflog = core.STATE_DIR / "firefox.log"
    core.say(f"  starting Firefox (its output goes to {fflog})")
    if core.DRY_RUN:
        core.run([core.FIREFOX_BIN])
        return
    core.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(fflog, "a") as handle:
        subprocess.Popen([core.FIREFOX_BIN], start_new_session=True, stdout=handle, stderr=handle,
                         stdin=subprocess.DEVNULL)
    for _ in range(60):
        time.sleep(2)
        if extension_ready():
            return


    # the row is context only: run_actions prints a note if the extension is not active yet
def policy_data() -> dict:
    try:
        return json.loads(core.FIREFOX_POLICIES.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def policy_ok() -> bool:
    entry = policy_data().get("policies", {}).get("ExtensionSettings", {}).get(core.XPI_ID, {})
    # force_installed is accepted too: ubuntu-tweaks' firefox-policy may manage this entry (credentialsd_helper)
    return entry.get("installation_mode") in ("normal_installed", "force_installed") and \
        entry.get("install_url") == XPI_URL


def install_policy() -> None:
    data = policy_data()
    data.setdefault("policies", {}).setdefault("ExtensionSettings", {})[core.XPI_ID] = {
        "installation_mode": "normal_installed",  # installed for you, but you can still disable/remove it
        "install_url": XPI_URL,
    }
    core.sudo_write(core.FIREFOX_POLICIES, json.dumps(data, indent=2) + "\n")


REGISTER_PROBE = (
    "import asyncio, sys\nfrom dbus_next.aio import MessageBus\nfrom dbus_next import Message\n"
    "async def main():\n"
    "    bus = await MessageBus().connect()\n"
    "    r = await bus.call(Message('org.freedesktop.portal.Desktop','/org/freedesktop/portal/desktop',"
    "'org.freedesktop.host.portal.Registry','Register',signature='sa{sv}',body=[sys.argv[1],{}]))\n"
    "    print('OK' if r.message_type.name == 'METHOD_RETURN' else 'ERR ' + str(r.body))\n"
    "asyncio.run(main())\n"
)


def portal_registry_ok() -> bool:
    """The helper must register its app id with the portal, otherwise credentialsd rejects every
    request ('App ID could not be verified'). The portal only accepts ids with a .desktop file."""
    if not credentialsd.portal_running():
        return False
    return core.out(["python3", "-c", REGISTER_PROBE, core.FIREFOX_APP_ID], timeout=30).endswith("OK")


FLATPAK_APPLICATION_DIRS = [Path("/var/lib/flatpak/exports/share/applications"),
                            core.HOME / ".local/share/flatpak/exports/share/applications"]


def app_desktop_exists() -> bool:
    """Is there already a desktop file with the app id (Flatpak Firefox, another package)? A copy in
    ~/.local/share/applications would shadow it: it would lose its menu entry and its Exec."""
    name = f"{core.FIREFOX_APP_ID}.desktop"
    dirs = [Path(d) / "applications" for d in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":") if d]
    dirs += FLATPAK_APPLICATION_DIRS
    return any((d / name).exists() for d in dirs)


def write_app_desktop() -> None:
    if app_desktop_exists():
        core.say(f"  {core.FIREFOX_APP_ID}.desktop already exists on this system: not shadowing it")
        return
    core.write_file(APP_DESKTOP, (
        "[Desktop Entry]\nVersion=1.0\nType=Application\nName=Firefox (portal app id)\n"
        "Comment=App id registered by the credentialsd native-messaging helper (passkey-qr); not shown in menus\n"
        "Exec=firefox %u\nIcon=firefox\nNoDisplay=true\nTerminal=false\n"))


USERJS_BEGIN = "// >>> passkey-qr managed >>>"
USERJS_END = "// <<< passkey-qr managed <<<"
USERJS_PREFS = (
    ("security.webauth.webauthn_enable_usbtoken", "false"),
    ("security.webauth.webauthn_enable_softtoken", "false"),
    ("security.webauth.u2f", "false"),
)


def userjs_block() -> str:
    prefs = "".join(f'user_pref("{name}", {value});\n' for name, value in USERJS_PREFS)
    return (f"{USERJS_BEGIN}\n// Firefox's own USB/soft-token WebAuthn path only shows a \"Touch your security key\" prompt;\n"
            f"// passkeys go through the credentialsd extension (QR).\n{prefs}{USERJS_END}\n")


def managed_profiles() -> list[Path]:
    """Existing profile dirs the deb Firefox may start: the install default and profiles.ini's Default=1."""
    names = [firefox.install_default_name()]
    cp = core.read_ini(core.MOZ_PROFILE / "profiles.ini")
    for section in cp.sections():
        if cp.get(section, "Default", fallback="0") == "1" and cp.has_option(section, "Path"):
            names.append(cp.get(section, "Path"))
    found: list[Path] = []
    for name in names:
        if not name:
            continue
        path = Path(name) if name.startswith("/") else core.MOZ_PROFILE / name
        if path.is_dir() and path not in found:
            found.append(path)
    return found


def userjs_with_block(text: str) -> str:
    """``text`` with the managed block added or replaced; every other line stays as it was."""
    block = userjs_block()
    start = text.find(USERJS_BEGIN)
    end = text.find(USERJS_END, start) if start != -1 else -1
    if start != -1 and end != -1:
        tail = text[end + len(USERJS_END):].lstrip("\n")
        return text[:start] + block + tail
    if start != -1:  # unterminated stale marker: drop only the marker, keep the foreign lines after it
        text = text.replace(USERJS_BEGIN + "\n", "").replace(USERJS_BEGIN, "")
    return text + ("" if not text or text.endswith("\n") else "\n") + block


def usb_token_off_ok() -> bool:
    profiles = managed_profiles()
    return bool(profiles) and all(
        userjs_block() in (core.read_text(profile / "user.js") or "") for profile in profiles)


def disable_usb_token() -> None:
    for profile in managed_profiles():
        path = profile / "user.js"
        core.write_file(path, userjs_with_block(core.read_text(path) or ""))
    core.say("  takes effect at the next Firefox start (restart Firefox once)")


def extension_actions() -> list[core.Action]:
    return [
        core.Action("Firefox never offers a USB security key (user.js)", usb_token_off_ok, disable_usb_token,
                    hint="no Firefox profile found to write user.js into: start Firefox once."),
        core.Action("native-messaging host linked for Firefox (replaces a manual manifest)", nm_ok, link_nm,
                    hint=lambda: f"{nm_target()} is missing: the credentialsd install step has to succeed first."),
        core.Action("Firefox policy installs the signed extension on next start", policy_ok, install_policy),
        core.Action("portal accepts the helper's app id (needs org.mozilla.firefox.desktop)", portal_registry_ok,
                    write_app_desktop, needs_relogin=True, hint="The portal rejects the helper's registration: see the log."),
        core.Action("Firefox restarted: extension active with access to all sites", extension_ready, restart_firefox,
                    required=False,
                    hint="Firefox did not report the extension as installed within 2 minutes: see "
                    f"{core.STATE_DIR}/firefox.log and about:policies / about:addons."),
    ]
