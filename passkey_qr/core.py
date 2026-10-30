"""Shared state, output, command running and the Action/step machinery every other module builds on."""

from __future__ import annotations

import configparser
import getpass
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NoReturn

HOME = Path.home()
SRC_DIR = Path(os.environ.get("PASSKEY_QR_SRC", HOME / "dev")).expanduser()
CACHE_DIR = HOME / ".cache/passkey-qr"

CREDSD_REPO = "https://github.com/linux-credentials/credentialsd"
CREDSD_TAG = "v0.3.1"

XPI_ID = "credentialsd-helper@iinuwa.xyz"

MOZ_PROFILE = HOME / ".mozilla/firefox"

FIREFOX_POLICIES = Path("/etc/firefox/policies/policies.json")

BLUEPRINT_MIN = (0, 20, 4)  # credentialsd-ui requires >= 0.20.4, Ubuntu 26.04 ships 0.19

CARGO_BIN = Path(os.environ.get("CARGO_HOME", HOME / ".cargo")) / "bin"  # where rustup puts cargo

FIREFOX_APP_ID = "org.mozilla.firefox"  # app id the credentialsd helper registers with the portal

DRY_RUN = False
ASSUME_YES = False
REMOVE_SNAP = False


class StepError(Exception):
    pass


class NeedsYou(Exception):
    """An unattended (--yes) run cannot go on with this step without a person (a Home Assistant token,
    Firefox closed). The rest of the step is skipped, the other steps still run, and `install` ends
    with exit code 3 and the list of what is waiting for you."""


NOTES: list[str] = []  # what is waiting for a person, printed at the end of `install`


def blocked(message: str) -> NoReturn:
    """Stop this step: an unattended (--yes) run waits for a person (exit 3), an interactive one fails."""
    if ASSUME_YES:
        raise NeedsYou(message)
    raise StepError(message)


STATE_DIR = Path(os.environ.get("PASSKEY_QR_STATE", HOME / ".local/state/passkey-qr"))
LOG_FILE = None  # opened by init_log()


def init_log(command: str) -> Path | None:
    """Mirror everything shown on screen (and all command output) into a log file."""
    global LOG_FILE
    path = STATE_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{command}-{os.getpid()}.log"
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        LOG_FILE = open(path, "a", buffering=1)
        if command == "install":  # latest.log always points to the last install run
            latest = STATE_DIR / "latest.log"
            latest.unlink(missing_ok=True)
            try:
                latest.symlink_to(path.name)
            except FileExistsError:  # another install started at the same moment: its link wins
                pass
    except OSError as err:
        print(f"warning: no log file ({err}); continuing without one", file=sys.stderr)
        LOG_FILE = None
        return None
    log(f"passkey-qr {command} started {time.strftime('%Y-%m-%d %H:%M:%S')} dry_run={DRY_RUN} user={getpass.getuser()}")
    return path


def log(text: str) -> None:
    if LOG_FILE:
        LOG_FILE.write(text if text.endswith("\n") else text + "\n")


def say(msg: str = "") -> None:
    print(msg, flush=True)
    log(msg)


def ask(prompt: str) -> str:
    log(f"{prompt}<waiting for input>")
    answer = input(prompt)
    log(f"  -> {answer!r}")
    return answer


def confirm(question: str, *, destructive: bool = False) -> bool:
    """Ask before a step. ``--yes`` answers for you and never prompts; a ``destructive`` step
    (removing the Firefox snap) additionally needs ``--remove-snap`` and is declined without it."""
    if destructive and ASSUME_YES and not REMOVE_SNAP:
        say(f"? {question} [no: --yes does not remove the snap without --remove-snap]")
        return False
    if ASSUME_YES:
        say(f"? {question} [yes: --yes]")
        return True
    if DRY_RUN:
        say(f"? {question} [auto-yes in dry-run]")
        return True
    while True:
        answer = ask(f"? {question} [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("", "n", "no"):
            return False


def _argv(cmd: list[str] | str, sudo: bool) -> list[str]:
    argv = shlex.split(cmd) if isinstance(cmd, str) else [str(a) for a in cmd]
    if sudo and os.geteuid() != 0:
        argv = ["sudo", *argv]
    return argv


def run_stream(cmd: list[str] | str, *, cwd: Path | None = None, sudo: bool = False) -> tuple[int, str]:
    """Run a command, echo its output live (screen and log) and also return it."""
    argv = _argv(cmd, sudo)
    say("  $ " + " ".join(shlex.quote(a) for a in argv))
    if DRY_RUN:
        return 0, ""
    started = time.time()
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                errors="replace")
    except OSError as err:  # a missing binary is a failed step with a log, not a traceback
        raise StepError(f"cannot run {argv[0]}: {err.strerror or err}") from err
    lines: list[str] = []
    assert proc.stdout
    for line in proc.stdout:
        sys.stdout.write(line)
        log(line)
        lines.append(line)
    rc = proc.wait()
    log(f"  [exit {rc} after {time.time() - started:.1f}s]")
    return rc, "".join(lines)


def run(cmd: list[str] | str, *, sudo: bool = False, cwd: Path | None = None,
        check: bool = True) -> subprocess.CompletedProcess:
    """Run a command, echo its output live and into the log (so the child sees a pipe, not a terminal:
    apt/meson progress bars are plain text; sudo still asks for its password on /dev/tty)."""
    argv = _argv(cmd, sudo)
    rc, text = run_stream(cmd, cwd=cwd, sudo=sudo)
    if check and rc != 0:
        raise subprocess.CalledProcessError(rc, argv, output=text)
    return subprocess.CompletedProcess(argv, rc, text, "")


def out(cmd: list[str], timeout: float = 20) -> str:
    try:
        return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def write_file(path: Path, content: str, mode: int = 0o644) -> None:
    say(f"  write {path} (mode {mode:o})")
    if DRY_RUN:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)


def sudo_write(path: Path, content: str) -> None:
    tmp = CACHE_DIR / "tmp" / path.name
    if not DRY_RUN:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(content)
    run(["install", "-D", "-m0644", str(tmp), str(path)], sudo=True)


def read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def http(url: str, timeout: float = 3.0) -> int:
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code
    except (urllib.error.URLError, OSError):
        return 0


@dataclass
class Action:
    name: str
    check: Callable[[], bool]
    apply: Callable[[], None]
    optional: bool = False  # confirmed separately; skipping it is not a failure
    destructive: bool = False  # with --yes it additionally needs --remove-snap
    required: bool = True   # False: context only; `check` ignores it and a failed re-check is not an error
    needs_relogin: bool = False  # under --yes a failed re-check waits for a new login (exit 3), not exit 1
    hint: str | Callable[[], str] = ""  # shown if the check still fails after apply (lazy when a callable)
    problem: Callable[[], str | None] = lambda: None  # why an unmet check is unmet, shown by status under the row


def show_actions(actions: list[Action]) -> list[Action]:
    pending = []
    for action in actions:
        ok = action.check()
        say(f"  [{'x' if ok else ' '}] {action.name}")
        if not ok:
            pending.append(action)
            if reason := action.problem():
                say(f"      {reason}")
    return pending


def run_actions(title: str, actions: list[Action], intro: str = "") -> bool:
    say(f"\n== {title} ==")
    if intro:
        say(intro)
    pending = show_actions(actions)
    if not pending:
        say("  nothing to do")
        return True
    if not confirm(f"Apply the {len(pending)} unchecked action(s) above?"):
        say("  skipped")
        return False
    for action in pending:
        if not _apply_pending(title, action):
            return False  # the later actions of this step depend on this one; other steps still run
    return True


def _apply_pending(title: str, action: Action) -> bool:
    """Apply one pending action; False when the remaining actions of the step must not run."""
    if action.check():  # an earlier action may already have fixed it
        return True
    if action.optional and not confirm(f"Optional: {action.name}?", destructive=action.destructive):
        say(f"  skipped: {action.name}")
        return True
    say(f"-> {action.name}")
    try:
        action.apply()
    except NeedsYou as note:
        NOTES.append(f"{title}: {note}")
        say(f"  needs you: {note}")
        return False
    if not DRY_RUN and not action.check():
        return _handle_failed_check(title, action)
    return True


def _handle_failed_check(title: str, action: Action) -> bool:
    hint = (action.hint() if callable(action.hint) else action.hint) or action.name
    if not action.required:
        say(f"  note: {action.name}: {hint}")
        return True
    if ASSUME_YES and action.needs_relogin:
        NOTES.append(f"{title}: {hint}")
        say(f"  needs you: {hint}")
        return False
    raise StepError(f"'{action.name}' was applied but the check still fails. {hint}".strip())


def service_active(unit: str, user: bool = False) -> bool:
    return out(["systemctl", *(["--user"] if user else []), "is-active", unit]) == "active"


def read_ini(path: Path) -> configparser.RawConfigParser:
    """Tolerant read for checks: a missing or unparsable file is just empty."""
    cp = configparser.RawConfigParser()
    cp.optionxform = str  # keep key case (Name, Path, Default, Locked)
    try:
        cp.read(path)
    except configparser.Error:
        return configparser.RawConfigParser()
    return cp


def read_ini_for_edit(path: Path) -> configparser.RawConfigParser:
    """Strict read before rewriting a user's file: never overwrite what cannot be parsed."""
    cp = configparser.RawConfigParser()
    cp.optionxform = str
    try:
        cp.read(path)
    except configparser.Error as err:
        raise StepError(f"{path} cannot be parsed ({err}); fix it by hand, nothing was changed") from err
    return cp


def write_ini(cp: configparser.RawConfigParser, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        cp.write(handle, space_around_delimiters=False)


FIREFOX_BIN = "/usr/lib/firefox/firefox"

EXIT_UNREACHABLE = 5
