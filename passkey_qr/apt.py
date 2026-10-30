"""apt packages: what is installed, installing, and finding the package behind a failed build's missing file."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from passkey_qr import core

APT_PACKAGES = [
    # toolchain
    "build-essential", "git", "curl", "ca-certificates", "rsync", "zip", "meson", "ninja-build", "pkg-config",
    "gettext",  # no rustc/cargo: credentialsd pins a newer Rust than Ubuntu ships, so rustup provides it
    # credentialsd (udev.pc lives in systemd-dev, not in libudev-dev)
    "libgtk-4-dev", "libadwaita-1-dev", "libdbus-1-dev", "libnfc-dev", "libpcsclite-dev",
    "libssl-dev", "libudev-dev", "systemd-dev", "python3-dbus-next",
    "python3-gi", "gir1.2-gtk-4.0", "gir1.2-adw-1",  # needed by blueprint-compiler
    "libclang-dev", "libxml2-utils",  # bindgen (nfc1-sys) needs clang headers; gresource needs xmllint
    # patched xdg-desktop-portal
    "libdex-dev", "libfuse3-dev", "libgdk-pixbuf-2.0-dev", "libgstreamer-plugins-base1.0-dev",
    "libpipewire-0.3-dev", "libwireplumber-0.5-dev", "libsystemd-dev", "libgudev-1.0-dev",
    "libjson-glib-dev", "libgeoclue-2-dev", "libflatpak-dev", "bubblewrap",
    # bluetooth stack
    "bluez",
]

# pkg-config / program names whose apt package is not found by the obvious name.
PKG_OVERRIDES = {"udev": "systemd-dev", "systemd": "systemd-dev", "ssl": "libssl-dev",
                 "xmllint": "libxml2-utils", "stdbool.h": "libclang-dev"}


def installed_apt() -> set[str]:
    status = core.out(["dpkg-query", "-W", "-f=${Package} ${db:Status-Abbrev}\n"], timeout=60)
    return {line.split()[0] for line in status.splitlines() if line.endswith("ii ")}


def missing_apt() -> list[str]:
    have = installed_apt()
    return [p for p in APT_PACKAGES if p not in have]


APT_QUIET = ["env", "DEBIAN_FRONTEND=noninteractive"]  # sudo drops the caller's environment: set it after sudo
APT_CONF = ["-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold"]


def apt_install(pkgs: list[str], extra: list[str] | None = None) -> None:
    """apt output goes through a pipe (see `run`), so a dpkg/debconf question without a newline would be
    invisible and look like a hang: answer them with the defaults instead."""
    core.run([*APT_QUIET, "apt-get", "install", "-y", *APT_CONF, *(extra or []), *pkgs], sudo=True)


DEP_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'ERROR: Dependency "([^"]+)" not found'), "pc"),
    (re.compile(r"Package '?([\w.+-]+)'? was not found in the pkg-config search path"), "pc"),
    (re.compile(r"The system library `([\w.+-]+)` required by crate"), "pc"),
    (re.compile(r"Program ([\w.+-]+) found: NO found [\d.]+ but need"), "tool"),
    (re.compile(r"ERROR: Program '([^']+)' not found"), "bin"),
    (re.compile(r"fatal error: ([\w./+-]+\.h): No such file or directory"), "hdr"),
    (re.compile(r"fatal error: '([\w./+-]+\.h)' file not found"), "hdr"),
    (re.compile(r"\b([\w.+-]+) is not in PATH"), "bin"),
]


def find_missing(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for pattern, kind in DEP_PATTERNS:
        for name in pattern.findall(text):
            if (kind, name) not in found:
                found.append((kind, name))
    return found


def apt_file_search(regex: str) -> list[str]:
    if not shutil.which("apt-file"):
        core.say("  apt-file is needed to find which package provides a missing file")
        apt_install(["apt-file"])
    if not any(Path("/var/cache/apt/apt-file").glob("*")):
        core.run(["apt-file", "update"], sudo=True)
    lines = core.out(["apt-file", "search", "--regexp", regex], timeout=120).splitlines()
    pkgs = [line.split(":")[0] for line in lines if ":" in line]
    pkgs = [p for p in pkgs if not p.endswith(("-dbgsym", "-doc"))]
    return sorted(pkgs, key=lambda p: (not p.endswith("-dev"), len(p)))[:1]


def _providers(kind: str, name: str) -> list[str]:
    if name in PKG_OVERRIDES:
        return [PKG_OVERRIDES[name]]
    if kind == "pc":
        return apt_file_search(rf"/pkgconfig/{re.escape(name)}\.pc$")
    if kind == "bin":
        return [name] if name in ("zip", "cargo", "rustc", "meson", "ninja") else \
            apt_file_search(rf"^/(usr/)?s?bin/{re.escape(name)}$")
    return apt_file_search(rf"/usr/include/(.*/)?{re.escape(name)}$")


def resolve_packages(missing: list[tuple[str, str]]) -> list[str]:
    pkgs: list[str] = []
    for kind, name in missing:
        if name in ("cargo", "rustc") and kind in ("bin", "tool"):
            core.say(f"  '{name}' comes from rustup (the credentialsd step), not from apt")
            continue
        found = _providers(kind, name)
        core.say(f"  missing {kind} '{name}' -> {', '.join(found) or 'no package found'}")
        pkgs += [p for p in found if p not in pkgs]
    return pkgs
