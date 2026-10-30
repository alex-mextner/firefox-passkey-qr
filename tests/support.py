"""Shared fixtures of the passkey-qr tests.

Run: python3 -m unittest discover -s tests
Nothing here touches the system: commands go through a recording `run`, Bluetooth comes from a
temporary directory (PASSKEY_QR_BLUETOOTH_DIR) and HOME is a temporary directory.

Every test module imports this one first: HOME and the Bluetooth directory are set before the package is
imported, because its path constants are computed at import. A test patches a name on the module that defines
it (`core.run`, `health.notify`); the package calls the names of other modules qualified, so that one patch is
seen by every caller.
"""

import ast
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "passkey-qr"
HOME = tempfile.mkdtemp()
BLUETOOTH = tempfile.mkdtemp()
os.environ["HOME"] = HOME
os.environ["PASSKEY_QR_BLUETOOTH_DIR"] = BLUETOOTH
sys.path.insert(0, str(ROOT))

from passkey_qr import (
    apt,
    bluetooth,
    build,
    cli,
    core,
    credentialsd,
    extension,
    firefox,
    health,
    selfheal,
)

MODULES = (core, apt, build, bluetooth, firefox, credentialsd, extension, selfheal, health, cli)
SENTINEL = "not-a-real-credential"


def bound_names(statement):
    """The names a module-level statement defines."""
    if isinstance(statement, (ast.FunctionDef, ast.ClassDef)):
        return {statement.name}
    if isinstance(statement, ast.Assign):
        targets = statement.targets
    elif isinstance(statement, ast.AnnAssign):
        targets = [statement.target]
    else:
        return set()
    return {n.id for target in targets for n in ast.walk(target) if isinstance(n, ast.Name)}


def defined_names(module):
    """The module-level names a module's source defines (not what is currently patched into it)."""
    names = set()
    for statement in ast.parse(Path(module.__file__).read_text()).body:
        names |= bound_names(statement)
    return names


DEFINED = {module: defined_names(module) for module in MODULES}


def owner(name):
    """The module whose source defines `name`; every other module reaches it as `<module>.<name>`."""
    defining = [module for module in MODULES if name in DEFINED[module]]
    assert len(defining) == 1, f"{name} is defined by {[m.__name__ for m in defining]}"
    return defining[0]


def patch_named(name, value):
    return mock.patch.object(owner(name), name, value)


def clear_bluetooth():
    for entry in Path(BLUETOOTH).iterdir():
        if entry.is_symlink() or entry.is_file():
            entry.unlink()
        else:
            shutil.rmtree(entry)


class Recorder:
    """Replaces run/write_file/say so a step can be executed without touching anything."""

    def __init__(self):
        self.commands = []
        self.files = []
        self.said = []

    def run(self, cmd, *, sudo=False, **_):
        argv = cmd.split() if isinstance(cmd, str) else list(cmd)
        self.commands.append((["sudo", *argv] if sudo else argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    def write_file(self, path, content, mode=0o644):
        self.files.append((path, mode))

    def say(self, message=""):
        self.said.append(message)

    @property
    def text(self):
        return "\n".join(self.said)


class Base(unittest.TestCase):
    def setUp(self):
        self.rec = Recorder()
        for name, target in (("run", self.rec.run), ("write_file", self.rec.write_file), ("say", self.rec.say)):
            patcher = patch_named(name, target)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.set_flags()
        self.addCleanup(self.set_flags)
        core.NOTES.clear()
        self.addCleanup(core.NOTES.clear)
        clear_bluetooth()

    def set_flags(self, dry_run=False, yes=False, remove_snap=False):
        core.DRY_RUN, core.ASSUME_YES, core.REMOVE_SNAP = dry_run, yes, remove_snap

    def patched(self, **values):
        for name, value in values.items():
            patcher = patch_named(name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class SelfHealingBase(Base):
    """Everything systemd/D-Bus/ps/journal is faked: `out` answers from a table, `run`/`say` are recorded."""

    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.patched(
            SYSTEMD_USER=self.root / "systemd",
            HEALTH_BIN=self.root / "libexec/passkey-qr",
            HEALTH_STATE=self.root / "state/health.json",
            STATE_DIR=self.root / "state",
            REQUEST_MARKER=self.root / "run/passkey-qr/request-pending",
            PASS_LOCK=self.root / "run/passkey-qr/pass.lock",
            HA_REPAIR_CONFIG=self.root / "config/passkey-qr/ha-repair",
            PROBE_RETRY_SECONDS=0,
        )
        self.answers = {"systemctl --user is-enabled": "enabled"}
        self.patched(out=self.fake_out)
        self.notices, self.bodies = [], []
        self.patched(notify=self.record_notice)
        self.real_ssh_docker = health.ssh_docker

    def record_notice(self, title, body, urgent=False):
        self.notices.append((title, urgent))
        self.bodies.append(body)

    def fake_out(self, cmd, timeout=20):
        for prefix, answer in self.answers.items():
            if " ".join(map(str, cmd)).startswith(prefix):
                return answer(cmd) if callable(answer) else answer
        return ""

    def loaded(self, *units, restart="always"):
        """Answer the batched `systemctl --user show -p Id -p LoadState -p Restart <units>` call."""

        def answer(cmd):
            return "\n\n".join(
                f"Id={name}\nLoadState={'loaded' if name[: -len('.service')] in units else 'not-found'}\n"
                f"Restart={restart if name[: -len('.service')] in units else 'no'}"
                for name in cmd[9:]
            )

        self.answers["systemctl --user show -p Id"] = answer

    def commands_starting(self, *words):
        return [c for c in self.rec.commands if c[: len(words)] == list(words)]
