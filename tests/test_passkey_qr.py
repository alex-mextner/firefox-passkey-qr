"""passkey-qr: the --yes/--remove-snap contract, `check`, and the Bluetooth branch of step 3.

Run: python3 -m unittest discover -s tests
Nothing here touches the system: commands go through a recording `run`, Bluetooth comes from a
temporary directory (PASSKEY_QR_BLUETOOTH_DIR) and HOME is a temporary directory.
"""

import dataclasses
import importlib.machinery
import importlib.util
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


def load():
    loader = importlib.machinery.SourceFileLoader("passkey_qr", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


pq = load()


def clear_bluetooth():
    for entry in Path(BLUETOOTH).iterdir():
        if entry.is_symlink() or entry.is_file():
            entry.unlink()
        else:
            shutil.rmtree(entry)
SENTINEL = "not-a-real-credential"


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
            patcher = mock.patch.object(pq, name, target)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.set_flags()
        self.addCleanup(self.set_flags)
        clear_bluetooth()

    def set_flags(self, dry_run=False, yes=False, remove_snap=False):
        pq.DRY_RUN, pq.ASSUME_YES, pq.REMOVE_SNAP = dry_run, yes, remove_snap

    def patched(self, **values):
        patcher = mock.patch.multiple(pq, **values)
        patcher.start()
        self.addCleanup(patcher.stop)


class Confirm(Base):
    def test_yes_never_prompts(self):
        self.set_flags(yes=True)
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")):
            self.assertTrue(pq.confirm("install?"))

    def test_yes_declines_the_destructive_step_without_remove_snap(self):
        self.set_flags(yes=True)
        self.assertFalse(pq.confirm("remove the snap?", destructive=True))
        self.set_flags(yes=True, remove_snap=True)
        self.assertTrue(pq.confirm("remove the snap?", destructive=True))

    def test_without_yes_the_user_is_asked(self):
        with mock.patch("builtins.input", side_effect=["", "nope", "y"]):
            self.assertFalse(pq.confirm("a?"))  # empty answer = no
            self.assertTrue(pq.confirm("a?", destructive=True))  # "nope" is re-asked, then "y"

    def test_dry_run_auto_yes_keeps_its_old_meaning(self):
        self.set_flags(dry_run=True)
        self.assertTrue(pq.confirm("a?"))
        self.assertTrue(pq.confirm("a?", destructive=True))


class BluetoothStep(Base):
    HA = ["http://100.64.0.7:8123"]

    def hci(self, name="hci0", virtual=False):
        target = Path(BLUETOOTH)
        if virtual:
            real = Path(tempfile.mkdtemp()) / "devices/virtual/bluetooth" / name
            real.mkdir(parents=True)
            (target / name).symlink_to(real)
        else:
            (target / name).mkdir()

    def bridge_commands(self):
        return [c for c in self.rec.commands if any("ha-ble-vhci" in part for part in c)]

    def test_a_local_adapter_skips_the_bridge(self):
        self.hci("hci0")
        self.patched(ha_candidates=mock.Mock(side_effect=AssertionError("looked for Home Assistant")))
        pq.step_bluetooth()
        self.assertEqual(self.rec.commands, [])
        self.assertIn("nothing to do", self.rec.text)

    def all_pending(self):
        """Every Bluetooth action reports 'not applied' whatever this machine looks like."""
        original = pq.bluetooth_actions
        self.patched(bluetooth_actions=lambda: [dataclasses.replace(a, check=lambda: False) for a in original()])

    def test_no_bluetooth_but_home_assistant_installs_the_bridge(self):
        self.set_flags(dry_run=True, yes=True)
        self.all_pending()
        self.patched(ha_candidates=lambda: self.HA, ha_token=lambda: SENTINEL, adapter_appears=lambda: None)
        pq.step_bluetooth()
        joined = [" ".join(c) for c in self.rec.commands]
        self.assertTrue(any("pip install" in c and pq.BRIDGE_REPO in c for c in joined), joined)
        self.assertIn("systemctl --user enable ha-ble-vhci", joined)
        self.assertIn("sudo modprobe hci_vhci", joined)
        self.assertIn((pq.BRIDGE_CFG / "token", 0o600), self.rec.files)
        self.assertIn((pq.BRIDGE_CFG / "env", 0o600), self.rec.files)

    def test_the_token_is_never_printed_or_put_on_a_command_line(self):
        self.set_flags(dry_run=True, yes=True)
        self.all_pending()
        self.patched(ha_candidates=lambda: self.HA, ha_token=lambda: SENTINEL, adapter_appears=lambda: None)
        pq.step_bluetooth()
        everything = self.rec.text + "\n".join(" ".join(c) for c in self.rec.commands)
        self.assertNotIn(SENTINEL, everything)

    def test_no_bluetooth_and_no_home_assistant_asks_for_a_dongle(self):
        self.set_flags(dry_run=True, yes=True)
        self.patched(ha_candidates=lambda: [], adapter_appears=lambda: None)
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")), self.assertRaises(pq.StepError) as raised:
            pq.step_bluetooth()
        self.assertIn("USB Bluetooth", str(raised.exception))

    def test_yes_without_a_token_stops_and_never_prompts(self):
        self.set_flags(dry_run=True, yes=True)
        self.patched(ha_candidates=lambda: self.HA, ha_token=lambda: None, adapter_appears=lambda: None)
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")), mock.patch(
            "getpass.getpass", side_effect=AssertionError("prompted")
        ), self.assertRaises(pq.StepError) as raised:
            pq.step_bluetooth()
        self.assertIn("--yes cannot ask for a token", str(raised.exception))
        self.assertEqual([p for p, _ in self.rec.files], [])

    def test_yes_does_not_retry_a_rejected_stored_token(self):
        self.set_flags(yes=True)
        self.patched(ha_url_configured=lambda: self.HA[0], read_text=lambda path: "old-token\n")
        with mock.patch.dict(os.environ, {}, clear=False) as env:
            env.pop("HA_TOKEN", None)
            with self.assertRaises(pq.StepError) as raised:
                pq.configure_ha()
        self.assertIn("--yes cannot ask for a token", str(raised.exception))
        self.assertEqual(self.rec.files, [])

    def test_yes_replaces_a_rejected_token_with_a_fresh_one_from_the_environment(self):
        self.set_flags(yes=True)
        self.patched(ha_url_configured=lambda: self.HA[0], read_text=lambda path: "old-token\n")
        with mock.patch.dict(os.environ, {"HA_TOKEN": SENTINEL}):
            pq.configure_ha()
        self.assertIn((pq.BRIDGE_CFG / "token", 0o600), self.rec.files)
        self.assertNotIn(SENTINEL, self.rec.text)


class Detection(Base):
    def test_adapters_come_from_the_configured_directory(self):
        self.assertEqual(pq.bt_adapters(), [])
        (Path(BLUETOOTH) / "hci0").mkdir()
        (Path(BLUETOOTH) / "rfkill0").mkdir()
        self.assertEqual(pq.bt_adapters(), ["hci0"])
        self.assertIsNone(pq.virtual_adapter())

    def test_virtual_adapter_is_recognised_by_its_sysfs_path(self):
        real = Path(tempfile.mkdtemp()) / "devices/virtual/bluetooth/hci1"
        real.mkdir(parents=True)
        (Path(BLUETOOTH) / "hci1").symlink_to(real)
        self.assertEqual(pq.virtual_adapter(), "hci1")


class FirefoxStep(Base):
    def test_a_running_firefox_stops_yes_instead_of_prompting(self):
        self.set_flags(yes=True)
        self.patched(snap_profile=lambda: Path(tempfile.mkdtemp()), firefox_running=lambda: True)
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")), self.assertRaises(pq.StepError):
            pq.copy_profile()
        self.assertEqual(self.rec.commands, [])

    def snap_action(self):
        state = {"present": True}

        def remove():
            state["present"] = False
            self.rec.run(["snap", "remove", "firefox"], sudo=True)

        return pq.Action("Firefox snap removed", lambda: not state["present"], remove, optional=True, destructive=True)

    def test_yes_keeps_the_snap_without_remove_snap(self):
        self.set_flags(yes=True)
        pq.run_actions("2. Firefox", [self.snap_action()])
        self.assertEqual(self.rec.commands, [])
        self.assertIn("skipped: Firefox snap removed", self.rec.text)

    def test_yes_with_remove_snap_removes_it(self):
        self.set_flags(yes=True, remove_snap=True)
        pq.run_actions("2. Firefox", [self.snap_action()])
        self.assertEqual(self.rec.commands, [["sudo", "snap", "remove", "firefox"]])

    def test_the_firefox_snap_action_is_marked_destructive(self):
        snap = [a for a in pq.firefox_actions() if "snap removed" in a.name]
        self.assertEqual([(a.optional, a.destructive) for a in snap], [(True, True)])


class Check(Base):
    def rows(self, *missing):
        names = ["Firefox (Mozilla deb)", "Bluetooth adapter (local or ha-ble-vhci)", "credentialsd installed"]
        return [(name, name not in missing, True) for name in names] + [("ha-ble-vhci service", False, False)]

    def run_check(self, rows):
        self.patched(status_rows=lambda: rows)
        with self.assertRaises(SystemExit) as raised:
            pq.cmd_check(None)
        return raised.exception.code

    def test_satisfied_ignores_context_rows(self):
        self.assertEqual(self.run_check(self.rows()), 0)

    def test_unmet_required_row_is_drift(self):
        self.assertEqual(self.run_check(self.rows("credentialsd installed")), 3)
        self.assertIn("missing: credentialsd installed", self.rec.text)


class CommandLine(unittest.TestCase):
    def run_cli(self, *args):
        env = {**os.environ, "HOME": HOME, "PASSKEY_QR_BLUETOOTH_DIR": BLUETOOTH}
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)

    def test_help_lists_the_new_flags_and_command(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        for word in ("--yes", "--remove-snap", "check"):
            self.assertIn(word, result.stdout)

    def test_check_without_bluetooth_is_drift_and_names_it(self):
        clear_bluetooth()
        result = self.run_cli("check")
        self.assertEqual(result.returncode, 3)
        self.assertIn("missing: Bluetooth adapter", result.stdout)


if __name__ == "__main__":
    unittest.main()
