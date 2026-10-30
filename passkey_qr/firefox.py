"""Step 2: Firefox from Mozilla's apt repo instead of the snap, and which profile Firefox starts with."""

from __future__ import annotations

import configparser
import re
import shutil
import subprocess
from pathlib import Path

from passkey_qr import apt, core

SNAP_PROFILE = core.HOME / "snap/firefox/common/.mozilla/firefox"

MOZ_KEY = Path("/etc/apt/keyrings/packages.mozilla.org.asc")
MOZ_LIST = Path("/etc/apt/sources.list.d/mozilla.list")
MOZ_PIN = Path("/etc/apt/preferences.d/mozilla")
APT_SOURCES_LIST = Path("/etc/apt/sources.list")
APT_SOURCES_DIR = Path("/etc/apt/sources.list.d")
APT_PREFS_DIR = Path("/etc/apt/preferences.d")
MOZ_REPO_URI = "https://packages.mozilla.org/apt"


def deb_firefox_installed() -> bool:
    version = core.out(["dpkg-query", "-W", "-f=${Version}", "firefox"])
    return Path("/usr/lib/firefox/firefox").exists() and bool(version) and "snap" not in version


def snap_firefox_present() -> bool:
    return Path("/snap/firefox/current").exists()


def firefox_running() -> bool:
    return bool(core.out(["pgrep", "-x", "firefox"]) or core.out(["pgrep", "-f", "/snap/firefox/"]))


def _moz_uri(uri: str) -> bool:
    return uri.rstrip("/").removeprefix("http://").removeprefix("https://") == MOZ_REPO_URI.removeprefix("https://")


def _moz_one_line(path: Path) -> list[str | None]:
    """Signed-By of every enabled one-line `deb`/`deb-src` entry for the Mozilla repo (None: no option)."""
    found: list[str | None] = []
    for raw in (core.read_text(path) or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        match = re.match(r"deb(?:-src)?\s+(?:\[([^\]]*)\]\s+)?(\S+)\s+(\S+)", line)
        if not match or not _moz_uri(match.group(2)) or match.group(3) != "mozilla":
            continue
        signed = re.search(r"(?:^|\s)signed-by=(\S+)", match.group(1) or "")
        found.append(signed.group(1) if signed else None)
    return found


def _deb822_fields(stanza: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    key = ""
    for line in stanza.splitlines():
        if line.startswith("#"):
            continue
        if line[:1].isspace() and key:
            fields[key] += " " + line.strip()
        elif ":" in line:
            key, _, value = line.partition(":")
            key = key.strip().lower()
            fields[key] = value.strip()
    return fields


def _moz_deb822(path: Path) -> list[str | None]:
    """Signed-By of every enabled deb822 stanza that names the Mozilla repo and suite."""
    found: list[str | None] = []
    for stanza in re.split(r"\n\s*\n", core.read_text(path) or ""):
        fields = _deb822_fields(stanza)
        if fields.get("enabled", "yes").lower() in ("no", "false", "0"):
            continue
        if any(_moz_uri(u) for u in fields.get("uris", "").split()) and "mozilla" in fields.get("suites", "").split():
            found.append(fields.get("signed-by") or None)
    return found


def mozilla_definitions() -> list[tuple[Path, str | None]]:
    """Every file that already defines the Mozilla apt repo, with its Signed-By: apt reads sources.list,
    *.list and *.sources, whoever wrote them (this installer or another tool)."""
    found: list[tuple[Path, str | None]] = []
    for path in [APT_SOURCES_LIST, *sorted(APT_SOURCES_DIR.glob("*.list"))]:
        found += [(path, signed) for signed in _moz_one_line(path)]
    for path in sorted(APT_SOURCES_DIR.glob("*.sources")):
        found += [(path, signed) for signed in _moz_deb822(path)]
    return found


def mozilla_pinned() -> bool:
    return any(re.search(r"^Pin:\s*origin\s+\"?packages\.mozilla\.org\"?\s*$", core.read_text(path) or "", re.M)
               for path in sorted(APT_PREFS_DIR.glob("*")) if path.is_file())


def mozilla_conflict() -> str | None:
    """The state in which apt fails system-wide (`Conflicting values set for option Signed-By`): the repo is
    defined more than once with different keys. Names the files and the removal that fixes it; deletes nothing."""
    defs = mozilla_definitions()
    if len({signed for _, signed in defs}) < 2:  # an unset Signed-By differs from a set one
        return None
    detail = "; ".join(f"{path} (Signed-By {signed or 'unset'})" for path, signed in defs)
    keep = next((signed for path, signed in defs if path != MOZ_LIST), defs[0][1])  # prefer another tool's file over ours
    extra = list(dict.fromkeys(path for path, signed in defs if signed != keep))  # what is left has one key
    return (f"packages.mozilla.org/apt is defined more than once with different keys, so apt fails with "
            f"'E: Conflicting values set for option Signed-By': {detail}. Keep one definition: "
            + "; ".join(f"sudo rm {path}" for path in extra))


def mozilla_repo_ok() -> bool:
    return (bool(mozilla_definitions()) and not mozilla_conflict() and mozilla_pinned()
            and "packages.mozilla.org" in core.out(["apt-cache", "policy", "firefox"], timeout=60))


def snap_profile() -> Path | None:
    ini = SNAP_PROFILE / "profiles.ini"
    if not ini.exists():
        return None
    cp = configparser.ConfigParser()
    cp.read(ini)
    paths = [(s.get("Path"), s.get("Default") == "1") for n, s in cp.items() if n.startswith("Profile") and s.get("Path")]
    paths.sort(key=lambda t: not t[1])
    for rel, _ in paths:
        if (SNAP_PROFILE / rel).is_dir():
            return SNAP_PROFILE / rel
    return None


def profile_copied() -> bool:
    prof = snap_profile()
    return prof is None or (core.MOZ_PROFILE / prof.name / "prefs.js").exists()


def install_mozilla_repo() -> None:
    conflict = mozilla_conflict()
    if conflict and core.DRY_RUN:
        core.say(f"  would stop: {conflict}")
        return
    if conflict:
        raise core.StepError(conflict + ". Nothing was changed; re-run `passkey-qr install` afterwards.")
    wrote = False
    if not mozilla_definitions():  # another tool's definition (any file, any key) already provides the repo
        core.run(["install", "-d", "-m0755", "/etc/apt/keyrings"], sudo=True)
        core.run(["curl", "-fsSL", "https://packages.mozilla.org/apt/repo-signing-key.gpg", "-o", str(MOZ_KEY)], sudo=True)
        core.sudo_write(MOZ_LIST, f"deb [signed-by={MOZ_KEY}] {MOZ_REPO_URI} mozilla main\n")
        wrote = True
    if not mozilla_pinned():
        core.sudo_write(MOZ_PIN, "Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n")
        wrote = True
    if not wrote:
        return
    try:
        core.run(["apt-get", "update"], sudo=True)
    except subprocess.CalledProcessError as err:
        conflict = mozilla_conflict()
        if conflict:
            raise core.StepError(f"apt-get update failed (exit {err.returncode}): {conflict}") from err
        raise


def need_firefox_closed() -> None:
    if firefox_running() and core.ASSUME_YES:  # also in --dry-run: the preview must show what the real run waits for
        raise core.NeedsYou("Firefox is running and --yes cannot ask you to close it: close it and re-run")
    if core.DRY_RUN:
        return
    while firefox_running():
        core.ask("  Close Firefox completely, then press Enter... ")


def copy_profile() -> None:
    prof = snap_profile()
    assert prof
    need_firefox_closed()
    core.run(["rsync", "-a", f"{prof}/", f"{core.MOZ_PROFILE / prof.name}/"])
    if not core.DRY_RUN:
        core.STATE_DIR.mkdir(parents=True, exist_ok=True)
        MIGRATED_NOTE.write_text(prof.name + "\n")
    if not (core.MOZ_PROFILE / "profiles.ini").exists():
        core.run(["cp", str(SNAP_PROFILE / "profiles.ini"), str(core.MOZ_PROFILE / "profiles.ini")])
    if not core.DRY_RUN:
        for lock in ("lock", ".parentlock"):
            (core.MOZ_PROFILE / prof.name / lock).unlink(missing_ok=True)


# Firefox names an install by a hash of its directory. Mozilla's deb lives in /usr/lib/firefox, so the hash is the
# same on every machine (it is also what the deb wrote on the first one). Seeding this section is how the
# deb adopts the migrated profile at its very first start, with no Firefox launched by the installer.
DEB_INSTALL_HASH = "4F96D1932A9F858E"


def install_default_name() -> str | None:
    """The profile the deb Firefox starts: its own section of installs.ini, else of profiles.ini. Other
    installs' sections (the snap's stale one, ESR, ...) say nothing about it."""
    inst = core.read_ini(core.MOZ_PROFILE / "installs.ini")
    if inst.has_option(DEB_INSTALL_HASH, "Default"):
        return inst.get(DEB_INSTALL_HASH, "Default")
    cp = core.read_ini(core.MOZ_PROFILE / "profiles.ini")
    return cp.get(f"Install{DEB_INSTALL_HASH}", "Default", fallback=None)


def active_profile() -> Path | None:
    name = install_default_name()
    return core.MOZ_PROFILE / name if name else None


MIGRATED_NOTE = core.STATE_DIR / "migrated-profile"
SWITCHED_NOTE = core.STATE_DIR / "profile-switched"  # written once the start profile was switched


def migrated_name() -> str | None:
    """Name of the profile copied from the snap, or None when there was no migration. Only evidence of
    one counts (the note written by the copy, or the snap's own profile): a legacy `Default=1` profile of
    a stock Firefox is not a migrated one."""
    snap = snap_profile()
    if snap:  # the source exists, so the copy is (or is about to be) made by the action before this one
        return snap.name
    try:
        note = MIGRATED_NOTE.read_text().strip()
    except OSError:
        return None
    return note if note and (core.MOZ_PROFILE / note / "prefs.js").exists() else None


def migrated_is_default() -> bool:
    """True when there is nothing to switch, or the switch was done: after it, a different default is the
    user's own choice in about:profiles and is neither drift nor reverted."""
    name = migrated_name()
    return name is None or install_default_name() == name or SWITCHED_NOTE.exists()


def _seed_install(cp: configparser.RawConfigParser, section: str, name: str) -> None:
    if not cp.has_section(section):
        cp.add_section(section)
    cp.set(section, "Default", name)
    cp.set(section, "Locked", "1")


def _backup_ini_files() -> None:
    for fname in ("profiles.ini", "installs.ini"):
        path = core.MOZ_PROFILE / fname
        backup = path.with_name(fname + ".bak-passkey-qr")
        if path.exists() and not backup.exists():  # the first backup is the user's original, keep it
            shutil.copy2(path, backup)


def _ensure_general(cp: configparser.RawConfigParser) -> None:
    if not cp.has_section("General"):
        cp.add_section("General")
        cp.set("General", "StartWithLastProfile", "1")
        cp.set("General", "Version", "2")


def _ensure_profile(cp: configparser.RawConfigParser, name: str) -> None:
    if any(cp.get(sec, "Path", fallback="") == name for sec in cp.sections()):
        return
    index = 0
    while cp.has_section(f"Profile{index}"):
        index += 1
    cp.add_section(f"Profile{index}")
    for key, value in (("Name", "migrated"), ("IsRelative", "1"), ("Path", name)):
        cp.set(f"Profile{index}", key, value)


def _mark_default_profile(cp: configparser.RawConfigParser, name: str) -> None:
    for sec in cp.sections():
        if not sec.startswith("Profile"):
            continue
        if cp.get(sec, "Path", fallback="") == name:
            cp.set(sec, "Default", "1")
        elif cp.has_option(sec, "Default"):
            cp.remove_option(sec, "Default")


def set_install_default() -> None:
    """Make the migrated snap profile the one the deb Firefox starts: by its own install section, written
    before its first start or switched after it created a fresh, locked profile. Other installs' sections
    are left alone."""
    name = migrated_name()
    assert name
    need_firefox_closed()
    if not core.DRY_RUN and not (core.MOZ_PROFILE / name / "prefs.js").exists():
        # e.g. the copy was declined in an interactive run: never point Firefox at a directory that is not there
        raise core.StepError(f"the profile {name} was not copied to {core.MOZ_PROFILE}: copy it first (answer yes to "
                             "'snap profile copied') and re-run")
    core.say(f"  start profile: {install_default_name() or '(not started yet)'} -> {name} "
             "(backups: *.bak-passkey-qr; a fresh profile is kept, nothing is deleted)")
    if core.DRY_RUN:
        return
    cp = core.read_ini_for_edit(core.MOZ_PROFILE / "profiles.ini")
    inst = core.read_ini_for_edit(core.MOZ_PROFILE / "installs.ini")
    _backup_ini_files()
    _ensure_general(cp)
    _ensure_profile(cp, name)
    _mark_default_profile(cp, name)
    _seed_install(cp, f"Install{DEB_INSTALL_HASH}", name)
    _seed_install(inst, DEB_INSTALL_HASH, name)
    core.MOZ_PROFILE.mkdir(parents=True, exist_ok=True)
    core.write_ini(cp, core.MOZ_PROFILE / "profiles.ini")
    core.write_ini(inst, core.MOZ_PROFILE / "installs.ini")
    core.STATE_DIR.mkdir(parents=True, exist_ok=True)
    SWITCHED_NOTE.write_text(name + "\n")


def firefox_actions() -> list[core.Action]:
    return [
        core.Action("Mozilla apt repo + pin (so apt does not bring the snap back)", mozilla_repo_ok, install_mozilla_repo,
                    hint=lambda: mozilla_conflict() or "", problem=mozilla_conflict),
        core.Action("Firefox deb installed",
                    deb_firefox_installed,
               # Ubuntu's transitional package has epoch 1:, so Mozilla's deb counts as a downgrade
                    lambda: apt.apt_install(["firefox"], extra=["--allow-downgrades"])),
        core.Action("snap profile copied to ~/.mozilla/firefox", profile_copied, copy_profile),
        core.Action("your migrated profile is the one Firefox starts (deb Firefox made a fresh one)",
                    migrated_is_default, set_install_default),
        core.Action("Firefox snap removed (snapd keeps an automatic snapshot)", lambda: not snap_firefox_present(),
                    lambda: core.run(["snap", "remove", "firefox"], sudo=True), optional=True, destructive=True),
    ]
