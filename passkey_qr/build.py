"""Building from source: build commands that fix their own missing dependencies, and the source-built blueprint-compiler."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable

from passkey_qr import apt, core

BLUEPRINT_REPO = "https://gitlab.gnome.org/GNOME/blueprint-compiler.git"
BLUEPRINT_TAG = "v0.20.4"

LOCAL_BIN = core.HOME / ".local/bin"
SYSTEM_PATH = ["/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"]

ERROR_LINE = re.compile(r"error|ERROR|FAILED|panicked|not found")  # what a failed build's excerpt keeps


def version_of(tool: str) -> tuple[int, ...]:
    match = re.search(r"(\d+(?:\.\d+)+)", core.out([tool, "--version"]))
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def ensure_local_bin_on_path() -> None:
    """~/.cargo/bin (rustup's cargo), ~/.local/bin (blueprint-compiler), then the system dirs, once, in that order.

    The system dirs beat the rest (Linuxbrew...) because every build here links apt's -dev packages: a brew python3
    has no `gi`, and meson would install blueprint-compiler into brew's site-packages under ~/.local.
    """
    wanted = [str(core.CARGO_BIN), str(LOCAL_BIN), *SYSTEM_PATH]
    rest = [p for p in os.environ.get("PATH", "").split(":") if p and p not in wanted]
    os.environ["PATH"] = ":".join(wanted + rest)


def blueprint_ok() -> bool:
    ensure_local_bin_on_path()
    return version_of("blueprint-compiler") >= core.BLUEPRINT_MIN


def build_blueprint() -> None:
    """Ubuntu's blueprint-compiler is too old for credentialsd-ui: build the pinned release into ~/.local."""
    src = core.SRC_DIR / "blueprint-compiler"
    if (src / ".git").exists():
        core.run(["git", "-C", str(src), "fetch", "--tags", "origin"])
    else:
        if not core.DRY_RUN:
            src.parent.mkdir(parents=True, exist_ok=True)
        core.run(["git", "clone", BLUEPRINT_REPO, str(src)])
    core.run(["git", "-C", str(src), "checkout", BLUEPRINT_TAG])
    # --wipe, not --reconfigure: a cached build dir keeps whichever python3 the first configure found
    run_resolving(["meson", "setup", "_build", "--prefix", str(core.HOME / ".local"), "--wipe"], src)
    run_resolving(["meson", "compile", "-C", "_build"], src)
    core.run(["meson", "install", "-C", "_build"], cwd=src)  # user prefix: no sudo
    ensure_local_bin_on_path()


SOURCE_BUILDERS: dict[str, Callable[[], None]] = {"blueprint-compiler": build_blueprint}


def _build_from_source(wanted: list[str], tried: set[str]) -> None:
    if not core.confirm(f"{', '.join(wanted)} must be built from source into ~/.local (Ubuntu's is too old). Do it?"):
        raise core.StepError("source-built tool declined")
    for name in wanted:
        SOURCE_BUILDERS[name]()
        tried.add(name)


def _unidentified_dependency(cmd: list[str], text: str) -> core.StepError:
    lines = text.splitlines()
    errs = [line for line in lines if ERROR_LINE.search(line)][:25]
    tail = "\n".join(errs + ["...", *lines[-8:]])
    return core.StepError(f"`{' '.join(cmd)}` failed and no installable dependency could be identified:\n{tail}")


def _install_missing(cmd: list[str], text: str, missing: list[tuple[str, str]], tried: set[str]) -> None:
    have = apt.installed_apt()
    pkgs = [p for p in apt.resolve_packages([m for m in missing if m[1] not in SOURCE_BUILDERS]) if p not in have]
    if not pkgs or set(pkgs) <= tried:
        raise _unidentified_dependency(cmd, text)
    if not core.confirm(f"Install the missing build dependencies ({' '.join(pkgs)}) with apt?"):
        raise core.StepError("build dependencies not installed")
    apt.apt_install(pkgs)
    tried |= set(pkgs)


def run_resolving(cmd: list[str], cwd: Path, rounds: int = 30) -> None:
    """Run a build command; when it fails on a missing dependency, install it and retry."""
    tried: set[str] = set()
    for _ in range(rounds):
        rc, text = core.run_stream(cmd, cwd=cwd)
        if rc == 0:
            return
        missing = apt.find_missing(text)
        wanted = [name for _, name in missing if name in SOURCE_BUILDERS and name not in tried]
        if wanted:  # tools Ubuntu ships too old: build the pinned release instead of apt
            _build_from_source(wanted, tried)
            continue
        _install_missing(cmd, text, missing, tried)
    raise core.StepError(f"`{' '.join(cmd)}` still failing after {rounds} rounds of installing dependencies")
