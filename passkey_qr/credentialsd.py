"""Step 4: credentialsd and the patched xdg-desktop-portal, built from source, and the portal configuration."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from passkey_qr import build, core

PORTAL_REPO = "https://github.com/linux-credentials/xdg-desktop-portal"
PORTAL_BRANCH = "credentials-portal"

PORTAL_CONF = core.HOME / ".config/xdg-desktop-portal/portals.conf"
CREDENTIAL_IMPL_IFACE = "org.freedesktop.impl.portal.experimental.Credential"

PORTAL_DROPIN = core.HOME / ".config/systemd/user/xdg-desktop-portal.service.d/credential.conf"
PORTAL_DROPIN_TEXT = "[Service]\nEnvironment=XDG_DESKTOP_PORTAL_ENABLE_EXPERIMENTAL=credential\n"


@dataclass
class Project:
    name: str
    repo: str
    ref: str
    is_tag: bool
    recurse: bool
    setup: list[str]
    build_dir: str

    @property
    def path(self) -> Path:
        return core.SRC_DIR / self.name

    @property
    def build(self) -> Path:
        return self.path / self.build_dir


PROJECTS = [
    Project("credentialsd", core.CREDSD_REPO, core.CREDSD_TAG, True, False, ["-Dprefix=/usr/local"], "build-release"),
    # Ubuntu's libdex is built without gdbus support, which the portal fork requires
    Project("libdex", "https://gitlab.gnome.org/GNOME/libdex.git", "1.2.0", True, False,
            ["--prefix", "/usr/local", "-Dgdbus=enabled", "-Dexamples=false", "-Dtests=false", "-Dvapi=false",
             "-Dintrospection=disabled", "-Dpygobject=false", "-Ddocs=false"], "_build"),
    Project("xdg-desktop-portal", PORTAL_REPO, PORTAL_BRANCH, False, True,
            ["--prefix", "/usr/local", "-Dtests=disabled", "-Ddocumentation=disabled", "-Dman-pages=disabled"], "_build"),
]


def git_out(path: Path, *args: str) -> str:
    return core.out(["git", "-C", str(path), *args])


def source_ready(p: Project) -> bool:
    if not (p.path / ".git").exists():
        return False
    at_ref = (git_out(p.path, "describe", "--tags", "--exact-match") == p.ref if p.is_tag
              else git_out(p.path, "rev-parse", "--abbrev-ref", "HEAD") == p.ref)
    subs = git_out(p.path, "submodule", "status", "--recursive").splitlines() if p.recurse else []
    return at_ref and not any(line[:1] in "-+U" for line in subs)


def fetch_source(p: Project) -> None:
    if (p.path / ".git").exists():
        core.run(["git", "-C", str(p.path), "fetch", "--tags", "origin"])
    else:
        p.path.parent.mkdir(parents=True, exist_ok=True)
        core.run(["git", "clone", *(["--recurse-submodules"] if p.recurse else []), p.repo, str(p.path)])
    core.run(["git", "-C", str(p.path), "checkout", p.ref])
    if p.recurse:
        core.run(["git", "-C", str(p.path), "submodule", "update", "--init", "--recursive"])


def installed_map(p: Project) -> dict[str, str]:
    if not (p.build / "meson-private").exists():
        return {}
    try:
        return json.loads(core.out(["meson", "introspect", "--installed", str(p.build)], timeout=60))
    except json.JSONDecodeError:
        return {}


def is_installed(p: Project) -> bool:
    files = installed_map(p)
    return bool(files) and all(os.path.lexists(dest) for dest in files.values())


def is_built(p: Project) -> bool:
    if is_installed(p):
        return True
    files = installed_map(p)
    return bool(files) and all(os.path.lexists(src) for src in files if src.startswith(str(p.build)))


def install_project(p: Project) -> None:
    """Install what was built. --no-rebuild: otherwise root re-runs the always-stale cargo/ninja
    targets with root's toolchain and leaves root-owned files in the user's build directory."""
    core.run(["meson", "install", "-C", p.build_dir, "--no-rebuild"], cwd=p.path, sudo=True)
    owner = f"{os.getuid()}:{os.getgid()}"
    core.run(["chown", "-R", owner, str(p.build)], sudo=True)  # undo any root-owned leftovers


def libdex_gdbus_ready() -> bool:
    return bool(core.out(["pkg-config", "--variable=gdbus_codegen_extension", "libdex-1"]))


def build_project(p: Project) -> None:
    build.ensure_local_bin_on_path()
    if p.name == "xdg-desktop-portal" and not core.DRY_RUN and not libdex_gdbus_ready():
        raise core.StepError("the portal needs libdex built with gdbus support installed system-wide "
                             "(pkg-config variable gdbus_codegen_extension is empty): install the libdex step first.")
    if p.name == "credentialsd" and rust_channel():
        os.environ["RUSTUP_TOOLCHAIN"] = rust_channel()  # overrides rust-toolchain.toml (no extra components)
    build.run_resolving(["meson", "setup", *p.setup, p.build_dir, "--reconfigure"], p.path)
    build.run_resolving(["meson", "compile", "-C", p.build_dir], p.path)


def system_portal_preferred() -> dict[str, str]:
    """The [preferred] backends of the portal config the desktop currently uses (to extend, not replace)."""
    desktops = [d.lower() for d in os.environ.get("XDG_CURRENT_DESKTOP", "").split(":") if d]
    dirs = [Path("/etc/xdg/xdg-desktop-portal"), Path("/usr/local/share/xdg-desktop-portal")]
    dirs += [Path(d) / "xdg-desktop-portal" for d in os.environ.get("XDG_DATA_DIRS", "/usr/share").split(":") if d]
    names = [f"{d}-portals.conf" for d in desktops] + ["portals.conf"]
    for directory in dirs:
        for name in names:
            cp = core.read_ini(directory / name)
            if cp.has_section("preferred"):
                return dict(cp.items("preferred"))
    return {"default": "*"}


def portal_conf_files() -> list[Path]:
    """Files the portal reads first for this desktop: `<desktop>-portals.conf` beats `portals.conf`, so an
    existing desktop-specific user file has to carry the entry as well."""
    desktops = [d.lower() for d in os.environ.get("XDG_CURRENT_DESKTOP", "").split(":") if d]
    specific = [PORTAL_CONF.with_name(f"{d}-portals.conf") for d in desktops]
    return [PORTAL_CONF, *[f for f in specific if f.exists()]]


def portal_conf_ok() -> bool:
    return all(core.read_ini(path).get("preferred", CREDENTIAL_IMPL_IFACE, fallback="") == "credentialsd"
               for path in portal_conf_files())


def write_portal_conf() -> None:
    """Without this the portal finds no backend for the Credential interface and never exports it:
    the desktop's own config (e.g. gnome-portals.conf) only lists gnome/gtk. Existing sections of the
    user's files are kept (their comments are not)."""
    for path in portal_conf_files():
        cp = core.read_ini_for_edit(path)
        if not cp.has_section("preferred"):
            cp.add_section("preferred")
            for key, value in system_portal_preferred().items():
                cp.set("preferred", key, value)
        cp.set("preferred", CREDENTIAL_IMPL_IFACE, "credentialsd")
        core.say(f"  write {path} (mode 644)")
        if not core.DRY_RUN:
            path.parent.mkdir(parents=True, exist_ok=True)
            core.write_ini(cp, path)


def credentialsd_activatable() -> bool:
    return "xyz.iinuwa.credentialsd" in core.out(["busctl", "--user", "list", "--activatable", "--no-pager"])


def portal_running() -> bool:
    """Introspecting an activatable name would start it: look at the owned names first, so `check` never does."""
    return "org.freedesktop.portal.Desktop" in core.out(["busctl", "--user", "list", "--acquired", "--no-pager"])


def portal_credential_ready() -> bool:
    if not portal_running():
        return False
    intro = core.out(["busctl", "--user", "introspect", "org.freedesktop.portal.Desktop",
                      "/org/freedesktop/portal/desktop", "--no-pager"], timeout=30)
    return "org.freedesktop.portal.experimental.Credential" in intro


def reload_user_services() -> None:
    core.run(["systemctl", "--user", "daemon-reload"])
    core.run(["systemctl", "--user", "reload", "dbus"], check=False)  # dbus-daemon re-reads service files


def restart_portal() -> None:
    reload_user_services()
    core.run(["systemctl", "--user", "restart", "xdg-desktop-portal"], check=False)  # no session: the re-check says so
    if not core.DRY_RUN:
        for _ in range(15):
            if portal_credential_ready():
                return
            time.sleep(1)


def libdex_linked() -> bool:
    return any("libdex-1" in line and "/usr/local" in line for line in core.out(["ldconfig", "-p"]).splitlines())


def rust_channel() -> str | None:
    text = core.read_text(core.SRC_DIR / "credentialsd" / "rust-toolchain.toml") or ""
    match = re.search(r'^channel\s*=\s*"([^"]+)"', text, re.M)
    return match.group(1) if match else None


def rust_ok() -> bool:
    channel = rust_channel()
    if not channel:
        return False
    build.ensure_local_bin_on_path()
    try:
        return subprocess.run([str(core.CARGO_BIN / "rustup"), "run", channel, "cargo", "--version"],
                              capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def install_rust() -> None:
    """credentialsd pins a Rust newer than Ubuntu's (its manifest needs TOML 1.1): use rustup in ~/.cargo."""
    # a dry run on a fresh machine has no credentialsd checkout yet: name where the pin will come from
    channel = rust_channel() or ("<channel pinned in credentialsd/rust-toolchain.toml>" if core.DRY_RUN else None)
    if not channel:
        raise core.StepError("credentialsd/rust-toolchain.toml not found: the credentialsd source step comes first")
    rustup = core.CARGO_BIN / "rustup"
    if not rustup.exists():
        script = core.CACHE_DIR / "rustup-init.sh"
        if not core.DRY_RUN:
            core.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        core.run(["curl", "--proto", "=https", "--tlsv1.2", "-sSf", "https://sh.rustup.rs", "-o", str(script)])
        core.run(["sh", str(script), "-y", "--profile", "minimal", "--default-toolchain", "none", "--no-modify-path"])
    core.run([str(rustup), "toolchain", "install", channel, "--profile", "minimal"])
    build.ensure_local_bin_on_path()


def credentialsd_actions() -> list[core.Action]:
    blueprint = core.Action(f"blueprint-compiler >= {'.'.join(map(str, core.BLUEPRINT_MIN))} (built into ~/.local; Ubuntu ships 0.19)",
                            build.blueprint_ok, build.build_blueprint)
    rust = core.Action("Rust toolchain pinned by credentialsd (via rustup in ~/.cargo; Ubuntu's cargo is too old)",
                       rust_ok, install_rust)
    actions: list[core.Action] = []
    for p in PROJECTS:
        actions.append(core.Action(f"{p.name}: source at {p.ref}" + (" with submodules" if p.recurse else ""),
                                   lambda p=p: source_ready(p), lambda p=p: fetch_source(p)))
        if p.name == "credentialsd":
            actions += [rust, blueprint]
        actions += [
            core.Action(f"{p.name}: built (missing build dependencies are installed automatically)",
                        lambda p=p: is_built(p), lambda p=p: build_project(p)),
            core.Action(f"{p.name}: installed into /usr/local", lambda p=p: is_installed(p),
                        lambda p=p: install_project(p)),
        ]
        if p.name == "libdex":
            actions.append(core.Action("libdex from /usr/local is known to the dynamic linker",
                                       libdex_linked, lambda: core.run(["ldconfig"], sudo=True)))
    actions.append(core.Action("xdg-desktop-portal: credential portal enabled (systemd user drop-in)",
                               lambda: core.read_text(PORTAL_DROPIN) == PORTAL_DROPIN_TEXT,
                               lambda: (core.write_file(PORTAL_DROPIN, PORTAL_DROPIN_TEXT),
                                        core.run(["systemctl", "--user", "daemon-reload"]))))
    actions += [
        core.Action("portal config names credentialsd as the Credential backend (~/.config/xdg-desktop-portal)",
                    portal_conf_ok, write_portal_conf),
        core.Action("session bus knows the credentialsd service files", credentialsd_activatable, reload_user_services,
                    needs_relogin=True,
                    hint="Log out and back in once so the session picks up the new activation files, then re-run."),
        core.Action("patched portal answers with the Credential interface", portal_credential_ready, restart_portal,
                    needs_relogin=True,
                    hint="Log out and back in once, then re-run; see the log for the portal journal."),
    ]
    return actions
