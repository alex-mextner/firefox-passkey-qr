"""passkey-qr: the --yes/--remove-snap contract, `check`, and the Bluetooth branch of step 3.

Run: python3 -m unittest discover -s tests
Nothing here touches the system: commands go through a recording `run`, Bluetooth comes from a
temporary directory (PASSKEY_QR_BLUETOOTH_DIR) and HOME is a temporary directory.
"""

import calendar
import dataclasses
import importlib.machinery
import importlib.util
import json
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
        pq.NOTES.clear()
        self.addCleanup(pq.NOTES.clear)
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

    def stateful(self):
        """Bluetooth actions that are 'not applied' until their apply ran (no machine state involved)."""
        original = pq.bluetooth_actions

        def build():
            actions = []
            for action in original():
                done = {"yes": False}

                def apply(action=action, done=done):
                    action.apply()
                    done["yes"] = True

                actions.append(dataclasses.replace(action, check=lambda done=done: done["yes"], apply=apply))
            return actions

        self.patched(bluetooth_actions=build)

    def test_no_bluetooth_and_no_home_assistant_waits_for_a_dongle_without_prompting(self):
        self.set_flags(yes=True)
        self.stateful()
        self.patched(ha_candidates=lambda: [], adapter_appears=lambda: None)
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")):
            self.assertFalse(pq.step_bluetooth())
        self.assertEqual(len(pq.NOTES), 1)
        self.assertIn("USB Bluetooth", pq.NOTES[0])
        self.assertEqual([p for p, _ in self.rec.files], [])

    def test_yes_without_a_token_waits_for_one_and_never_prompts(self):
        self.set_flags(yes=True)
        self.stateful()
        self.patched(ha_candidates=lambda: self.HA, ha_token=lambda: None, adapter_appears=lambda: None)
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")), mock.patch(
            "getpass.getpass", side_effect=AssertionError("prompted")
        ):
            self.assertFalse(pq.step_bluetooth())
        self.assertIn("needs a long-lived admin token", pq.NOTES[0])
        self.assertEqual([p for p, _ in self.rec.files], [])
        # the actions that come before the token (installing the bridge) did run
        self.assertTrue(self.bridge_commands())

    def test_dry_run_with_yes_lists_the_bridge_without_a_token(self):
        self.set_flags(dry_run=True, yes=True)
        self.all_pending()
        self.patched(ha_candidates=lambda: self.HA, ha_token=lambda: None, adapter_appears=lambda: None)
        pq.step_bluetooth()
        self.assertEqual(pq.NOTES, [])
        self.assertIn("systemctl --user enable ha-ble-vhci", [" ".join(c) for c in self.rec.commands])

    def test_yes_does_not_retry_a_rejected_stored_token(self):
        self.set_flags(yes=True)
        self.patched(ha_url_configured=lambda: self.HA[0], read_text=lambda path: "old-token\n")
        with mock.patch.dict(os.environ, {}, clear=False) as env:
            env.pop("HA_TOKEN", None)
            with self.assertRaises(pq.NeedsYou) as raised:
                pq.configure_ha()
        self.assertIn("needs a long-lived admin token", str(raised.exception))
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
    def test_a_running_firefox_makes_yes_wait_instead_of_prompting(self):
        self.set_flags(yes=True)
        self.patched(snap_profile=lambda: Path(tempfile.mkdtemp()), firefox_running=lambda: True)
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")), self.assertRaises(pq.NeedsYou):
            pq.copy_profile()
        self.assertEqual(self.rec.commands, [])

    def test_switching_the_start_profile_also_waits_for_a_closed_firefox(self):
        self.set_flags(yes=True)
        self.patched(migrated_name=lambda: "x.default", firefox_running=lambda: True)
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")), self.assertRaises(pq.NeedsYou):
            pq.set_install_default()

    def test_yes_never_closes_or_starts_firefox(self):
        self.set_flags(yes=True)
        with mock.patch("subprocess.Popen", side_effect=AssertionError("started Firefox")):
            pq.restart_firefox()
        self.assertEqual(self.rec.commands, [])
        self.assertIn("not restarted by --yes", self.rec.text)

    def test_a_dry_run_with_yes_previews_exactly_what_yes_does_to_firefox(self):
        self.set_flags(yes=True, dry_run=True)
        self.patched(firefox_running=lambda: True)
        pq.restart_firefox()
        self.assertEqual(self.rec.commands, [])  # no pkill, no launch in the preview either
        self.assertIn("not restarted by --yes", self.rec.text)

    def test_an_interactive_dry_run_with_firefox_open_does_not_wait_for_it_to_close(self):
        self.set_flags(dry_run=True)
        calls = []
        self.patched(firefox_running=lambda: calls.append(1) or True)
        with mock.patch("time.sleep", side_effect=AssertionError("waited")):
            pq.restart_firefox()
        self.assertIn(["pkill", "-TERM", "-x", "firefox"], self.rec.commands)
        self.assertEqual(self.rec.commands[-1], [pq.FIREFOX_BIN])

    def test_yes_never_opens_the_test_page(self):
        self.set_flags(yes=True)
        with mock.patch("subprocess.Popen", side_effect=AssertionError("opened a tab")):
            pq.open_test_page()

    def test_the_restart_row_is_context_not_a_requirement(self):
        row = [a for a in pq.extension_actions() if a.name.startswith("Firefox restarted")]
        self.assertEqual([a.required for a in row], [False])

    def test_an_entry_managed_by_ubuntu_tweaks_counts_as_the_policy(self):
        entry = {"installation_mode": "force_installed", "install_url": pq.XPI_URL}
        self.patched(policy_data=lambda: {"policies": {"ExtensionSettings": {pq.XPI_ID: entry}}})
        self.assertTrue(pq.policy_ok())
        self.patched(policy_data=lambda: {"policies": {"ExtensionSettings": {pq.XPI_ID: {**entry, "install_url": "x"}}}})
        self.assertFalse(pq.policy_ok())

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


SNAP_INI = """[General]
StartWithLastProfile=1
Version=2

[Profile0]
Name=default
IsRelative=1
Path=snap.default
Default=1
"""
DEB_INI = """[General]
StartWithLastProfile=1
Version=2

[Profile0]
Name=default
IsRelative=1
Path=snap.default
Default=1

[Install4F96D1932A9F858E]
Default=fresh.default-release
Locked=1

[Profile1]
Name=default-release
IsRelative=1
Path=fresh.default-release
"""
INSTALLS_INI = """[4F96D1932A9F858E]
Default=fresh.default-release
Locked=1
"""


class FirefoxProfiles(unittest.TestCase):
    """The deb Firefox creates and locks a fresh profile; the copy of the snap profile must become the one it
    starts. These rewrite the user's profiles.ini, so they run against temporary files."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.moz = self.root / "firefox"
        (self.moz / "snap.default").mkdir(parents=True)
        (self.moz / "snap.default" / "prefs.js").write_text("// the user's profile\n")
        (self.moz / "fresh.default-release").mkdir()
        for name, value in dict(MOZ_PROFILE=self.moz, STATE_DIR=self.root / "state",
                                MIGRATED_NOTE=self.root / "state" / "migrated-profile",
                                SWITCHED_NOTE=self.root / "state" / "profile-switched",
                                SNAP_PROFILE=self.root / "no-snap").items():
            patcher = mock.patch.object(pq, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in dict(firefox_running=lambda: False, say=lambda *a, **k: None).items():
            patcher = mock.patch.object(pq, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        pq.DRY_RUN = pq.ASSUME_YES = False
        self.addCleanup(lambda: setattr(pq, "ASSUME_YES", False))
        self.addCleanup(lambda: setattr(pq, "DRY_RUN", False))

    def write(self, name, text):
        (self.moz / name).write_text(text)

    def note_migration(self, name="snap.default"):
        """What copy_profile leaves behind: the evidence that this profile came from the snap."""
        pq.MIGRATED_NOTE.parent.mkdir(parents=True, exist_ok=True)
        pq.MIGRATED_NOTE.write_text(name + "\n")

    def test_the_switch_is_pending_on_the_first_run_before_the_profile_was_copied(self):
        # step 2 computes its pending actions up front: the copy does not exist yet, the snap profile does
        snap = self.root / "snap-fx"
        (snap / "snap.default").mkdir(parents=True)
        (snap / "snap.default" / "prefs.js").write_text("//")
        (snap / "profiles.ini").write_text(SNAP_INI)
        with mock.patch.object(pq, "SNAP_PROFILE", snap):
            shutil.rmtree(self.moz / "snap.default")  # not copied yet
            self.assertEqual(pq.migrated_name(), "snap.default")
            self.assertFalse(pq.migrated_is_default())

    def test_a_dry_run_with_yes_shows_a_running_firefox_as_waiting(self):
        pq.DRY_RUN = pq.ASSUME_YES = True
        with mock.patch.object(pq, "firefox_running", lambda: True), self.assertRaises(pq.NeedsYou):
            pq.need_firefox_closed()

    def test_a_machine_without_firefox_profiles_has_nothing_to_switch(self):
        self.assertIsNone(pq.install_default_name())
        self.assertTrue(pq.migrated_is_default())

    def test_before_the_debs_first_start_the_install_is_seeded_so_it_adopts_the_migrated_profile(self):
        # the copied snap profiles.ini has the snap's own [Install<hash>] (its hash is not the deb's) and no
        # installs.ini exists yet: the deb would create a fresh profile on its first start
        self.write("profiles.ini", DEB_INI.replace(
            "[Install4F96D1932A9F858E]\nDefault=fresh.default-release", "[Install1234567890ABCDEF]\nDefault=snap.default"))
        self.note_migration()
        self.assertIsNone(pq.install_default_name())  # the snap's section says nothing about the deb
        self.assertFalse(pq.migrated_is_default())
        pq.set_install_default()
        self.assertEqual(pq.install_default_name(), "snap.default")
        self.assertTrue(pq.migrated_is_default())
        installs = (self.moz / "installs.ini").read_text()
        self.assertIn("[4F96D1932A9F858E]", installs)
        self.assertIn("Locked=1", installs)
        self.assertIn("[Install1234567890ABCDEF]", (self.moz / "profiles.ini").read_text())  # untouched

    def test_a_machine_that_never_started_firefox_gets_a_complete_profiles_ini(self):
        (self.moz / "profiles.ini").unlink(missing_ok=True)
        self.note_migration()
        pq.set_install_default()
        text = (self.moz / "profiles.ini").read_text()
        self.assertIn("StartWithLastProfile=1", text)
        self.assertIn("Path=snap.default", text)
        self.assertEqual(pq.install_default_name(), "snap.default")

    def test_the_install_default_is_the_profile_firefox_starts(self):
        self.write("profiles.ini", DEB_INI)
        self.write("installs.ini", INSTALLS_INI)
        self.note_migration()
        self.assertEqual(pq.install_default_name(), "fresh.default-release")
        self.assertEqual(pq.active_profile(), self.moz / "fresh.default-release")
        self.assertEqual(pq.migrated_name(), "snap.default")  # the copy, found without the snap's own directory
        self.assertFalse(pq.migrated_is_default())

    def test_switching_points_both_files_at_the_copy_and_keeps_backups_and_the_fresh_profile(self):
        self.write("profiles.ini", DEB_INI)
        self.write("installs.ini", INSTALLS_INI)
        self.note_migration()
        pq.set_install_default()
        self.assertEqual(pq.install_default_name(), "snap.default")
        self.assertTrue(pq.migrated_is_default())
        self.assertIn("Default=snap.default", (self.moz / "installs.ini").read_text())
        self.assertIn("Default=fresh.default-release", (self.moz / "installs.ini.bak-passkey-qr").read_text())
        self.assertTrue((self.moz / "profiles.ini.bak-passkey-qr").exists())
        self.assertTrue((self.moz / "fresh.default-release").is_dir())  # nothing is deleted
        again = (self.moz / "profiles.ini").read_text()
        pq.set_install_default()  # idempotent
        self.assertEqual((self.moz / "profiles.ini").read_text(), again)

    def test_a_profile_missing_from_profiles_ini_gets_a_section(self):
        self.write("profiles.ini", "[General]\nVersion=2\n\n[Install4F96D1932A9F858E]\nDefault=fresh.default-release\n")
        self.write("installs.ini", INSTALLS_INI)
        self.note_migration()
        pq.set_install_default()
        self.assertEqual(pq.install_default_name(), "snap.default")
        self.assertIn("Path=snap.default", (self.moz / "profiles.ini").read_text())

    def test_a_running_firefox_blocks_the_switch_before_anything_is_written(self):
        self.write("profiles.ini", DEB_INI)
        self.write("installs.ini", INSTALLS_INI)
        self.note_migration()
        pq.ASSUME_YES = True
        with mock.patch.object(pq, "firefox_running", lambda: True), self.assertRaises(pq.NeedsYou):
            pq.set_install_default()
        self.assertEqual((self.moz / "profiles.ini").read_text(), DEB_INI)
        self.assertFalse((self.moz / "profiles.ini.bak-passkey-qr").exists())


    def test_the_snaps_own_install_section_is_not_a_second_install(self):
        snap_copy = DEB_INI.replace("[Profile1]", "[Install1234567890ABCDEF]\nDefault=snap.default\nLocked=1\n\n[Profile1]")
        self.write("profiles.ini", snap_copy)
        self.write("installs.ini", INSTALLS_INI)
        self.note_migration()
        pq.ASSUME_YES = True
        pq.set_install_default()  # must not wait for a person
        self.assertEqual(pq.install_default_name(), "snap.default")

    def test_a_legacy_default_profile_of_a_stock_firefox_is_not_a_migrated_one(self):
        self.write("profiles.ini", DEB_INI)  # Default=1 sits on snap.default, as in any Firefox upgraded from pre-67
        self.write("installs.ini", INSTALLS_INI)
        self.assertIsNone(pq.migrated_name())  # no note, no snap profile: no evidence of a migration
        self.assertTrue(pq.migrated_is_default())  # so there is nothing to switch

    def test_the_switch_refuses_a_profile_that_was_never_copied(self):
        self.write("profiles.ini", DEB_INI)
        self.write("installs.ini", INSTALLS_INI)
        self.note_migration("not-copied.default")  # the note names a directory that is not there
        with mock.patch.object(pq, "migrated_name", lambda: "not-copied.default"), self.assertRaises(pq.StepError):
            pq.set_install_default()
        self.assertEqual((self.moz / "installs.ini").read_text(), INSTALLS_INI)
        self.assertFalse(pq.SWITCHED_NOTE.exists())

    def test_a_later_choice_of_the_user_is_not_drift_and_is_not_reverted(self):
        self.write("profiles.ini", DEB_INI)
        self.write("installs.ini", INSTALLS_INI)
        self.note_migration()
        pq.set_install_default()
        (self.moz / "installs.ini").write_text(INSTALLS_INI)  # the user picks the fresh profile again
        self.assertEqual(pq.install_default_name(), "fresh.default-release")
        self.assertTrue(pq.migrated_is_default())  # switched once; their choice stands

    def test_the_first_backup_is_the_users_original_and_is_never_overwritten(self):
        self.write("profiles.ini", DEB_INI)
        self.write("installs.ini", INSTALLS_INI)
        self.note_migration()
        pq.set_install_default()
        # something moves the default again, then the installer runs once more
        (self.moz / "installs.ini").write_text(INSTALLS_INI.replace("fresh.default-release", "elsewhere"))
        pq.set_install_default()
        self.assertIn("fresh.default-release", (self.moz / "installs.ini.bak-passkey-qr").read_text())

    def test_other_installs_keep_their_profile(self):
        other = "\n[0123456789ABCDEF]\nDefault=other.default\nLocked=1\n"  # an ESR / tarball install
        self.write("profiles.ini", DEB_INI)
        self.write("installs.ini", INSTALLS_INI + other)
        self.note_migration()
        pq.set_install_default()
        installs = (self.moz / "installs.ini").read_text()
        self.assertIn("[0123456789ABCDEF]\nDefault=other.default", installs)
        self.assertEqual(pq.install_default_name(), "snap.default")

    def test_an_unparsable_profiles_ini_is_left_alone(self):
        broken = "[Profile0]\nPath=a\nPath=b\n"  # duplicate option
        self.write("profiles.ini", broken)
        self.note_migration()
        with self.assertRaises(pq.StepError):
            pq.set_install_default()
        self.assertEqual((self.moz / "profiles.ini").read_text(), broken)
        self.assertIsNone(pq.install_default_name())  # checks stay tolerant


class BridgeVersion(Base):
    def venv(self, version):
        """A fake bridge venv whose `pip show` reports `version` (None: not installed)."""
        root = Path(tempfile.mkdtemp())
        (root / "venv/bin").mkdir(parents=True)
        if version:
            pip = root / "venv/bin/pip"
            pip.write_text(f"#!/bin/sh\necho 'Name: ha-ble-vhci'\necho 'Version: {version}'\n")
            pip.chmod(0o755)
            (root / "venv/bin/ha-ble-vhci").write_text("#!/bin/sh\n")
        self.patched(BRIDGE_DIR=root, BRIDGE_BIN=root / "venv/bin/ha-ble-vhci")

    def test_versions_compare_as_numbers(self):
        self.assertGreater(pq.version_tuple("0.1.10"), pq.version_tuple("0.1.9"))
        self.assertEqual(pq.version_tuple("0.2"), pq.version_tuple("0.2.0"))
        self.assertEqual(pq.version_tuple("0.1.6+local"), pq.version_tuple("0.1.6"))

    def test_a_pre_release_is_older_than_its_release(self):
        for pre in ("0.1.6rc1", "0.1.6.dev0", "0.1.6a1", "0.1.6b2"):
            self.assertLess(pq.version_tuple(pre), pq.version_tuple("0.1.6"), pre)
            self.assertGreater(pq.version_tuple(pre), pq.version_tuple("0.1.5"), pre)
        self.assertLess(pq.version_tuple("0.2.0rc1"), pq.version_tuple("0.2"))

    def test_a_post_release_is_newer_than_its_release_and_never_downgraded(self):
        self.assertGreater(pq.version_tuple("0.1.6.post1"), pq.version_tuple("0.1.6"))
        self.assertGreater(pq.version_tuple("0.1.6post2"), pq.version_tuple("0.1.6"))
        with mock.patch.object(pq, "BRIDGE_VERSION", "0.1.6"):
            self.venv("0.1.6.post1")
            self.assertTrue(pq.bridge_current())

    def test_a_pre_release_of_the_pinned_version_is_upgraded(self):
        with mock.patch.object(pq, "BRIDGE_VERSION", "0.1.6"):
            self.venv("0.1.6rc1")
            self.assertFalse(pq.bridge_current())
            self.venv("0.1.6")
            self.assertTrue(pq.bridge_current())
            self.venv("0.1.6.0")
            self.assertTrue(pq.bridge_current())

    def test_a_newer_bridge_is_current_and_an_older_or_missing_one_is_not(self):
        pinned = pq.BRIDGE_VERSION
        newer = ".".join([*pinned.split(".")[:-1], str(int(pinned.split(".")[-1]) + 1)])
        self.venv(newer)
        self.assertTrue(pq.bridge_current())
        self.venv(pinned)
        self.assertTrue(pq.bridge_current())
        self.venv("0.0.1")
        self.assertFalse(pq.bridge_current())
        self.venv(None)
        self.assertFalse(pq.bridge_current())

    def test_a_newer_bridge_is_never_reinstalled_or_restarted(self):
        self.venv("99.0.0")
        self.set_flags(yes=True)
        action = next(a for a in pq.bluetooth_actions() if "or newer installed" in a.name)
        pq.run_actions("3. Bluetooth", [action])
        self.assertEqual(self.rec.commands, [])  # no pip, no systemctl restart

    def test_an_older_bridge_is_upgraded_to_the_pinned_version(self):
        self.venv("0.0.1")
        self.set_flags(yes=True)
        action = next(a for a in pq.bluetooth_actions() if "or newer installed" in a.name)
        action = dataclasses.replace(action, apply=lambda: pq.run(["pip", "install", "ha-ble-vhci"]))
        with self.assertRaises(pq.StepError):  # the recorded run does not change the fake venv: re-check fails
            pq.run_actions("3. Bluetooth", [action])
        self.assertIn(["pip", "install", "ha-ble-vhci"], self.rec.commands)


class Spawning(Base):
    def test_a_missing_binary_is_a_failed_step_not_a_traceback(self):
        self.patched(run_stream=pq.run_stream)  # the real one: Base replaces run, not run_stream
        with self.assertRaises(pq.StepError) as raised:
            pq.run_stream(["/nonexistent/binary-for-test"])
        self.assertIn("cannot run /nonexistent/binary-for-test", str(raised.exception))


class Probes(Base):
    def test_check_never_starts_the_portal(self):
        calls = []

        def fake_out(cmd, timeout=20):
            calls.append(cmd)
            return ""  # the portal's name is not owned

        self.patched(out=fake_out)
        self.assertFalse(pq.portal_credential_ready())
        self.assertFalse(pq.portal_registry_ok())
        self.assertTrue(all(c[:3] == ["busctl", "--user", "list"] for c in calls), calls)  # nothing introspected

    def test_a_log_that_cannot_be_written_never_fails_the_install(self):
        with mock.patch.object(pq, "STATE_DIR", Path("/proc/nonexistent/state")), mock.patch.object(pq, "LOG_FILE", None):
            self.assertIsNone(pq.init_log("install"))


class UsbTokenOff(unittest.TestCase):
    """user.js managed block: Firefox must never offer a USB security key (tmp profile dirs only)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.moz = self.root / "firefox"
        (self.moz / "snap.default").mkdir(parents=True)
        (self.moz / "fresh.default-release").mkdir()
        for name, value in dict(MOZ_PROFILE=self.moz, say=lambda *a, **k: None).items():
            patcher = mock.patch.object(pq, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        pq.DRY_RUN = False
        self.write("profiles.ini", f"[Install{pq.DEB_INSTALL_HASH}]\nDefault=fresh.default-release\nLocked=1\n"
                                   "[Profile0]\nName=snap\nIsRelative=1\nPath=snap.default\nDefault=1\n")
        self.userjs = self.moz / "fresh.default-release" / "user.js"

    def write(self, name, text):
        (self.moz / name).write_text(text)

    def test_profiles_are_the_install_default_and_the_profiles_ini_default(self):
        self.assertEqual(pq.managed_profiles(),
                         [self.moz / "fresh.default-release", self.moz / "snap.default"])

    def test_missing_pref_is_pending_and_apply_writes_it(self):
        self.assertFalse(pq.usb_token_off_ok())
        pq.disable_usb_token()
        self.assertTrue(pq.usb_token_off_ok())
        for profile in pq.managed_profiles():
            text = (profile / "user.js").read_text()
            for name in ("webauthn_enable_usbtoken", "webauthn_enable_softtoken", "u2f"):
                self.assertRegex(text, rf'user_pref\("security\.webauth\.{name}", false\);')

    def test_one_profile_without_it_keeps_the_row_pending(self):
        pq.disable_usb_token()
        (self.moz / "snap.default" / "user.js").unlink()
        self.assertFalse(pq.usb_token_off_ok())

    def test_no_profile_means_not_ok(self):
        shutil.rmtree(self.moz / "fresh.default-release")
        shutil.rmtree(self.moz / "snap.default")
        self.assertFalse(pq.usb_token_off_ok())

    def test_apply_is_idempotent(self):
        pq.disable_usb_token()
        first = self.userjs.read_text()
        pq.disable_usb_token()
        self.assertEqual(self.userjs.read_text(), first)
        self.assertEqual(first.count(pq.USERJS_BEGIN), 1)

    def test_foreign_lines_survive_before_and_after_the_block(self):
        self.userjs.write_text('user_pref("my.pref", 1);\n')
        pq.disable_usb_token()
        self.userjs.write_text(self.userjs.read_text() + 'user_pref("later.pref", 2);')
        pq.disable_usb_token()
        text = self.userjs.read_text()
        self.assertIn('user_pref("my.pref", 1);', text)
        self.assertIn('user_pref("later.pref", 2);', text)
        self.assertEqual(text.count(pq.USERJS_BEGIN), 1)

    def test_a_stale_block_is_replaced(self):
        self.userjs.write_text(f'keep\n{pq.USERJS_BEGIN}\nuser_pref("security.webauth.webauthn_enable_usbtoken", true);\n'
                               f'{pq.USERJS_END}\nkeep2\n')
        self.assertFalse(pq.usb_token_off_ok())
        pq.disable_usb_token()
        text = self.userjs.read_text()
        self.assertNotIn("usbtoken\", true", text)
        self.assertTrue(pq.usb_token_off_ok())
        self.assertIn("keep\n", text)
        self.assertIn("keep2\n", text)

    def test_it_is_a_required_row_for_check(self):
        action = next(a for a in pq.extension_actions() if "USB security key" in a.name)
        self.assertTrue(action.required)


class PortalConfig(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for name, value in dict(PORTAL_CONF=self.root / "portals.conf", say=lambda *a, **k: None).items():
            patcher = mock.patch.object(pq, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        pq.DRY_RUN = False
        patcher = mock.patch.object(pq, "system_portal_preferred",
                                    lambda: {"default": "gnome;gtk;", "org.freedesktop.impl.portal.Secret": "gnome-keyring;"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_desktops_own_backends_are_kept_and_credentialsd_is_added(self):
        self.assertFalse(pq.portal_conf_ok())
        pq.write_portal_conf()
        self.assertTrue(pq.portal_conf_ok())
        text = pq.PORTAL_CONF.read_text()
        self.assertIn("default=gnome;gtk;", text)
        self.assertIn("org.freedesktop.impl.portal.Secret=gnome-keyring;", text)
        self.assertIn("org.freedesktop.impl.portal.experimental.Credential=credentialsd", text)

    def test_an_existing_config_without_a_preferred_section_gets_the_desktop_defaults(self):
        pq.PORTAL_CONF.write_text("# empty on purpose\n[other]\nx=1\n")
        pq.write_portal_conf()
        self.assertTrue(pq.portal_conf_ok())
        self.assertIn("default=gnome;gtk;", pq.PORTAL_CONF.read_text())
        self.assertIn("[other]", pq.PORTAL_CONF.read_text())  # the user's other sections stay

    def test_a_desktop_specific_user_file_carries_the_entry_too(self):
        specific = pq.PORTAL_CONF.with_name("gnome-portals.conf")
        specific.write_text("[preferred]\ndefault=gnome\n")
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}):
            self.assertFalse(pq.portal_conf_ok())
            pq.write_portal_conf()
            self.assertTrue(pq.portal_conf_ok())
        self.assertIn("org.freedesktop.impl.portal.experimental.Credential=credentialsd", specific.read_text())
        self.assertIn("default=gnome", specific.read_text())

    def test_an_unparsable_portal_config_is_not_overwritten(self):
        pq.PORTAL_CONF.write_text("[preferred]\na=1\na=2\n")
        with self.assertRaises(pq.StepError):
            pq.write_portal_conf()
        self.assertEqual(pq.PORTAL_CONF.read_text(), "[preferred]\na=1\na=2\n")

    def test_an_existing_user_config_is_extended_not_replaced(self):
        pq.PORTAL_CONF.write_text("[preferred]\ndefault=kde\nmy.own.Interface=mine\n")
        pq.write_portal_conf()
        text = pq.PORTAL_CONF.read_text()
        self.assertIn("default=kde", text)
        self.assertIn("my.own.Interface=mine", text)
        self.assertNotIn("gnome", text)
        self.assertTrue(pq.portal_conf_ok())


class ProxyStatus(Base):
    """`status` shows the live proxy line (bridge --scanners, subprocess mocked); `check` ignores it."""

    def setUp(self):
        super().setUp()
        self.patched(BRIDGE_BIN=Path(HOME) / "bridge-bin", BRIDGE_CFG=Path(HOME) / "bridge-cfg")
        pq.BRIDGE_CFG.mkdir(exist_ok=True)
        pq.BRIDGE_BIN.write_text("")
        (pq.BRIDGE_CFG / "token").write_text("secret")
        (pq.BRIDGE_CFG / "env").write_text("HA_URL=http://ha:8123\n")
        self.addCleanup(pq.BRIDGE_BIN.unlink)
        self.addCleanup(shutil.rmtree, pq.BRIDGE_CFG, True)

    def scanners(self, returncode, stdout=""):
        return mock.patch.object(
            pq.subprocess, "run", return_value=subprocess.CompletedProcess([], returncode, stdout, ""))

    def test_online_proxy_is_listed(self):
        out = "btproxy-kitchen (30:C9:22:D1:A5:72)  source=30:C9:22:D1:A5:72\n"
        with self.scanners(0, out) as run:
            pq.show_proxy_status()
        self.assertIn("[x] a Bluetooth proxy is online in Home Assistant (btproxy-kitchen (30:C9:22:D1:A5:72))",
                      self.rec.text)
        argv = run.call_args.args[0]
        self.assertEqual(argv[1:], ["--scanners", "--url", "http://ha:8123", "--token-file", str(pq.BRIDGE_CFG / "token")])
        self.assertIn("timeout", run.call_args.kwargs)

    def test_no_proxy_prints_an_actionable_hint(self):
        with self.scanners(4):
            pq.show_proxy_status()
        self.assertIn("[ ] a Bluetooth proxy is online", self.rec.text)
        self.assertIn("power on / reconnect the ESP32", self.rec.text)
        self.assertIn("journalctl --user -u ha-ble-vhci", self.rec.text)

    def test_unanswerable_query_prints_nothing(self):
        with self.scanners(1):
            pq.show_proxy_status()
        with mock.patch.object(pq.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 30)):
            pq.show_proxy_status()
        self.assertEqual(self.rec.said, [])

    def test_status_shows_it_only_when_the_bridge_provides_bluetooth(self):
        self.patched(status_groups=lambda: [("3. Bluetooth via Home Assistant", [])], missing_apt=lambda: [])
        with self.scanners(0, "p  source=1\n"):
            self.patched(real_adapters=lambda: [])
            pq.cmd_status(None)
            self.assertIn("a Bluetooth proxy is online", self.rec.text)
            self.rec.said.clear()
            self.patched(real_adapters=lambda: ["hci0"])
            pq.cmd_status(None)
        self.assertNotIn("proxy", self.rec.text)

    def test_check_does_not_ask(self):
        def guarded(argv, *args, **kwargs):
            self.assertNotIn("--scanners", argv)
            return subprocess.CompletedProcess(argv, 1, "", "")

        with mock.patch.object(pq.subprocess, "run", side_effect=guarded):
            pq.status_rows()


class Dependencies(unittest.TestCase):
    def test_a_too_old_tool_is_recognised_as_a_source_built_tool(self):
        log = "Program blueprint-compiler found: NO found 0.19.0 but need: '>= 0.20.4' (/usr/bin/blueprint-compiler)"
        self.assertEqual(pq.find_missing(log), [("tool", "blueprint-compiler")])
        self.assertIn("blueprint-compiler", pq.SOURCE_BUILDERS)

    def test_hyphenated_tool_names_are_captured_whole(self):
        self.assertEqual(pq.find_missing("glib-compile-resources is not in PATH"), [("bin", "glib-compile-resources")])
        self.assertEqual(pq.find_missing("blueprint-compiler is not in PATH"), [("bin", "blueprint-compiler")])

    def test_missing_system_pieces_map_to_apt_packages_without_apt_file(self):
        self.assertEqual(pq.find_missing('ERROR: Dependency "udev" not found, tried pkgconfig'), [("pc", "udev")])
        self.assertEqual(pq.find_missing("x.h:1:10: fatal error: 'stdbool.h' file not found"), [("hdr", "stdbool.h")])
        self.assertEqual(pq.find_missing("xmllint is not in PATH"), [("bin", "xmllint")])
        with mock.patch.object(pq, "say", lambda *a, **k: None):
            self.assertEqual(pq.resolve_packages([("pc", "udev"), ("hdr", "stdbool.h"), ("bin", "xmllint")]),
                             ["systemd-dev", "libclang-dev", "libxml2-utils"])

    def test_cargo_and_rustc_come_from_rustup_never_from_apt(self):
        with mock.patch.object(pq, "say", lambda *a, **k: None), mock.patch.object(
                pq, "apt_file_search", side_effect=AssertionError("searched apt for cargo")):
            self.assertEqual(pq.resolve_packages([("bin", "cargo"), ("bin", "rustc")]), [])

    def test_the_rust_check_follows_the_exit_status_for_any_channel_spelling(self):
        for channel in ("1.97.1", "stable", "nightly-2026-01-01", "1.97.1-x86_64-unknown-linux-gnu"):
            with mock.patch.object(pq, "rust_channel", lambda c=channel: c), mock.patch.object(
                    pq.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"cargo 1.97.1\n", b"")) as run:
                self.assertTrue(pq.rust_ok(), channel)
                self.assertEqual(run.call_args.args[0][2:4], [channel, "cargo"])
        with mock.patch.object(pq, "rust_channel", lambda: "stable"), mock.patch.object(
                pq.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"toolchain not installed")):
            self.assertFalse(pq.rust_ok())

    def test_local_tool_directories_come_first_exactly_once(self):
        with mock.patch.dict(os.environ, {"PATH": f"/usr/bin:{pq.LOCAL_BIN}:/bin"}):
            pq.ensure_local_bin_on_path()
            pq.ensure_local_bin_on_path()
            self.assertEqual(os.environ["PATH"], f"{pq.CARGO_BIN}:{pq.LOCAL_BIN}:/usr/bin:/bin")


class RunActions(Base):
    def test_needs_you_skips_the_rest_of_the_step_and_is_reported(self):
        calls = []

        def blocked():
            raise pq.NeedsYou("close Firefox")

        actions = [pq.Action("a", lambda: False, blocked), pq.Action("b", lambda: False, lambda: calls.append("b"))]
        self.set_flags(yes=True)
        self.assertFalse(pq.run_actions("2. Firefox", actions))
        self.assertEqual(calls, [])
        self.assertEqual(pq.NOTES, ["2. Firefox: close Firefox"])

    def test_a_context_action_that_stays_unsatisfied_is_not_an_error(self):
        action = pq.Action("c", lambda: False, lambda: None, required=False, hint="later")
        self.set_flags(yes=True)
        self.assertTrue(pq.run_actions("5. x", [action]))
        self.assertIn("note: c: later", self.rec.text)

    def test_a_required_action_that_stays_unsatisfied_is_an_error(self):
        self.set_flags(yes=True)
        with self.assertRaises(pq.StepError):
            pq.run_actions("5. x", [pq.Action("d", lambda: False, lambda: None)])


class ExtensionStep(Base):
    def test_the_policy_is_written_before_anything_that_waits_for_a_new_login(self):
        names = [a.name for a in pq.extension_actions()]
        policy = next(i for i, n in enumerate(names) if n.startswith("Firefox policy"))
        relogin = [i for i, a in enumerate(pq.extension_actions()) if a.needs_relogin]
        self.assertTrue(relogin)
        self.assertLess(policy, min(relogin))

    def test_an_existing_desktop_file_with_the_app_id_is_never_shadowed(self):
        exports = Path(tempfile.mkdtemp())
        (exports / "applications").mkdir()
        (exports / "applications" / f"{pq.FIREFOX_APP_ID}.desktop").write_text("[Desktop Entry]\n")
        with mock.patch.dict(os.environ, {"XDG_DATA_DIRS": str(exports)}):
            self.assertTrue(pq.app_desktop_exists())
            pq.write_app_desktop()
        self.assertEqual(self.rec.files, [])
        with mock.patch.dict(os.environ, {"XDG_DATA_DIRS": str(Path(tempfile.mkdtemp()))}), mock.patch.object(
                pq, "HOME", Path(tempfile.mkdtemp())), mock.patch.object(pq, "FLATPAK_APPLICATION_DIRS", []):
            pq.write_app_desktop()
        self.assertEqual([p for p, _ in self.rec.files], [pq.APP_DESKTOP])

    def test_apt_never_waits_for_a_question_nobody_can_see(self):
        pq.apt_install(["zip"])
        command = self.rec.commands[-1]
        self.assertEqual(command[:4], ["sudo", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get"])
        self.assertIn("Dpkg::Options::=--force-confold", command)


class Relogin(Base):
    def test_a_check_that_needs_a_new_login_waits_for_it_under_yes_instead_of_failing(self):
        action = pq.Action("portal answers", lambda: False, lambda: None, needs_relogin=True, hint="log out and in")
        self.set_flags(yes=True)
        self.assertFalse(pq.run_actions("4. credentialsd", [action]))
        self.assertEqual(pq.NOTES, ["4. credentialsd: log out and in"])

    def test_the_same_check_is_an_error_when_a_person_is_present(self):
        action = pq.Action("portal answers", lambda: False, lambda: None, needs_relogin=True, hint="log out and in")
        with mock.patch("builtins.input", return_value="y"), self.assertRaises(pq.StepError):
            pq.run_actions("4. credentialsd", [action])

    def test_a_callable_hint_is_only_evaluated_when_the_check_fails(self):
        hint = mock.Mock(return_value="late")
        self.set_flags(yes=True)
        pq.run_actions("5. x", [pq.Action("ok after apply", iter([False, True, True]).__next__, lambda: None, hint=hint)])
        hint.assert_not_called()


class FailureKeepsNotes(unittest.TestCase):
    def test_a_later_failure_still_lists_what_already_waits_for_you(self):
        script = Path(HOME) / "fail.py"
        script.write_text(
            "import importlib.machinery, importlib.util, sys\n"
            f"loader = importlib.machinery.SourceFileLoader('pq', {str(SCRIPT)!r})\n"
            "spec = importlib.util.spec_from_loader('pq', loader); pq = importlib.util.module_from_spec(spec)\n"
            "sys.modules['pq'] = pq; loader.exec_module(pq)\n"
            "pq.NOTES.append('3. Bluetooth: needs a token')\n"
            "def boom(ns): raise pq.StepError('credentialsd build failed')\n"
            "sys.argv = ['passkey-qr', '--dry-run', 'status']\n"
            "pq.cmd_status = boom\n"
            "pq.main()\n")
        result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60,
                                env={**os.environ, "HOME": HOME})
        self.assertEqual(result.returncode, 1)
        self.assertIn("credentialsd build failed", result.stderr)
        self.assertIn("still waiting for you: 3. Bluetooth: needs a token", result.stderr)


class MozillaRepo(Base):
    """The repo step must not write a second definition of packages.mozilla.org: apt refuses to read sources
    that give one repository two different Signed-By values. All apt paths are redirected to a temp tree."""

    LIST_LINE = "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main\n"
    SOURCES = ("# Managed by ubuntu-tweaks (apt-sources)\nTypes: deb\nURIs: https://packages.mozilla.org/apt\n"
               "Suites: mozilla\nComponents: main\nSigned-By: /etc/apt/keyrings/mozilla.asc\n")
    PIN = "Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n"

    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.d = self.root / "sources.list.d"
        self.prefs = self.root / "preferences.d"
        self.d.mkdir()
        self.prefs.mkdir()
        self.patched(APT_SOURCES_LIST=self.root / "sources.list", APT_SOURCES_DIR=self.d, APT_PREFS_DIR=self.prefs,
                     MOZ_LIST=self.d / "mozilla.list", MOZ_KEY=self.root / "packages.mozilla.org.asc",
                     MOZ_PIN=self.prefs / "mozilla", CACHE_DIR=self.root / "cache",
                     out=lambda cmd, timeout=20: "packages.mozilla.org")

    def write(self, path, text):
        path.write_text(text)
        return path

    def pin(self):
        self.write(self.prefs / "other-name", "Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n")

    def written(self):
        return [Path(c[5]) for c in self.rec.commands if c[:3] == ["sudo", "install", "-D"]]

    def updated(self):
        return ["sudo", "apt-get", "update"] in self.rec.commands

    def test_a_definition_in_sources_list_satisfies_the_repo(self):
        self.write(self.root / "sources.list", "# c\n" + self.LIST_LINE.replace("deb [", "deb [arch=amd64 ").replace("main\n", "main # mozilla\n"))
        self.pin()
        self.assertTrue(pq.mozilla_repo_ok())
        pq.install_mozilla_repo()
        self.assertEqual(self.rec.commands, [])

    def test_a_list_file_under_another_name_and_key_satisfies_the_repo(self):
        self.write(self.d / "firefox-extra.list", "deb-src [signed-by=/k/other.gpg] https://packages.mozilla.org/apt/ mozilla main\n")
        self.pin()
        self.assertTrue(pq.mozilla_repo_ok())
        pq.install_mozilla_repo()
        self.assertEqual(self.rec.commands, [])

    def test_a_deb822_file_satisfies_the_repo(self):
        self.write(self.d / "mozilla.sources", self.SOURCES)
        self.pin()
        self.assertTrue(pq.mozilla_repo_ok())
        pq.install_mozilla_repo()
        self.assertEqual(self.rec.commands, [])

    def test_deb822_multi_value_uris_and_suites_count(self):
        self.write(self.d / "m.sources", "Types: deb\nURIs: https://example.org/x https://packages.mozilla.org/apt/\n"
                   "Suites: stable mozilla\nComponents: main\n")
        self.assertEqual([p.name for p, _ in pq.mozilla_definitions()], ["m.sources"])

    def test_things_that_are_not_a_definition_do_not_count(self):
        self.write(self.root / "sources.list", "# " + self.LIST_LINE)
        self.write(self.d / "off.sources", self.SOURCES.replace("Types: deb\n", "Types: deb\nEnabled: no\n"))
        self.write(self.d / "host.list", "deb https://packages.example.org/apt mozilla main\n")
        self.write(self.d / "suite.list", "deb https://packages.mozilla.org/apt other main\n")
        self.write(self.d / "notes.txt", self.LIST_LINE)
        self.write(self.d / "stanza.sources", "Types: deb\nURIs: https://example.org\nSuites: mozilla\n\n"
                   "Types: deb\nURIs: https://packages.mozilla.org/apt\nSuites: other\n")
        self.assertEqual(pq.mozilla_definitions(), [])
        self.pin()
        self.assertFalse(pq.mozilla_repo_ok())

    def test_a_pin_in_any_preferences_file_counts_and_a_commented_one_does_not(self):
        self.write(self.d / "mozilla.sources", self.SOURCES)
        self.write(self.prefs / "x", "# Pin: origin packages.mozilla.org\n")
        self.assertFalse(pq.mozilla_repo_ok())
        self.pin()
        self.assertTrue(pq.mozilla_repo_ok())

    def test_the_repo_is_not_ok_while_apt_does_not_know_it(self):
        self.write(self.d / "mozilla.sources", self.SOURCES)
        self.pin()
        self.patched(out=lambda cmd, timeout=20: "")
        self.assertFalse(pq.mozilla_repo_ok())

    def test_nothing_defined_writes_key_list_and_pin_then_updates(self):
        pq.install_mozilla_repo()
        self.assertEqual(self.written(), [pq.MOZ_LIST, pq.MOZ_PIN])
        self.assertTrue(any(c[:2] == ["sudo", "curl"] for c in self.rec.commands))
        self.assertTrue(self.updated())

    def test_a_definition_without_a_pin_writes_only_the_pin(self):
        self.write(self.d / "mozilla.sources", self.SOURCES)
        pq.install_mozilla_repo()
        self.assertEqual(self.written(), [pq.MOZ_PIN])
        self.assertFalse(any(c[:2] == ["sudo", "curl"] for c in self.rec.commands))

    def conflict(self):
        self.write(self.d / "mozilla.sources", self.SOURCES)
        self.write(self.d / "mozilla.list", self.LIST_LINE)
        self.pin()

    def test_two_definitions_with_different_keys_are_reported_with_the_exact_fix_and_nothing_is_changed(self):
        self.conflict()
        self.assertFalse(pq.mozilla_repo_ok())
        with self.assertRaises(pq.StepError) as raised:
            pq.install_mozilla_repo()
        message = str(raised.exception)
        self.assertIn(str(self.d / "mozilla.sources"), message)
        self.assertIn(str(self.d / "mozilla.list"), message)
        self.assertIn(f"sudo rm {self.d / 'mozilla.list'}", message)
        self.assertEqual(self.rec.commands, [])
        self.assertTrue((self.d / "mozilla.list").exists() and (self.d / "mozilla.sources").exists())

    def test_status_shows_the_conflict_under_the_row(self):
        self.conflict()
        action = pq.firefox_actions()[0]
        pq.show_actions([action])
        self.assertIn(f"sudo rm {self.d / 'mozilla.list'}", self.rec.text)

    def test_check_names_the_conflict(self):
        self.conflict()
        self.patched(status_rows=lambda: [(pq.firefox_actions()[0].name, False, True)])
        with self.assertRaises(SystemExit):
            pq.cmd_check(None)
        self.assertIn(f"sudo rm {self.d / 'mozilla.list'}", self.rec.text)

    def test_two_definitions_with_the_same_key_are_not_a_conflict(self):
        self.write(self.d / "a.list", self.LIST_LINE)
        self.write(self.d / "b.list", self.LIST_LINE)
        self.assertIsNone(pq.mozilla_conflict())

    def test_a_definition_without_signed_by_next_to_one_with_it_is_a_conflict(self):
        self.write(self.d / "a.list", "deb https://packages.mozilla.org/apt mozilla main\n")
        self.write(self.d / "b.list", self.LIST_LINE)
        self.assertIn("sudo rm", pq.mozilla_conflict() or "")

    def test_three_definitions_name_every_file_that_has_to_go(self):
        self.write(self.d / "mozilla.sources", self.SOURCES)
        self.write(self.d / "other.sources", self.SOURCES.replace("mozilla.asc", "other.asc"))
        self.write(self.d / "mozilla.list", self.LIST_LINE)
        message = pq.mozilla_conflict() or ""
        self.assertIn(f"sudo rm {self.d / 'mozilla.list'}", message)
        self.assertIn(f"sudo rm {self.d / 'other.sources'}", message)
        self.assertNotIn(f"sudo rm {self.d / 'mozilla.sources'}", message)

    def test_a_failing_update_names_the_conflict_cause(self):
        def failing(cmd, *, sudo=False, **_):
            self.rec.run(cmd, sudo=sudo)
            if cmd[:2] == ["apt-get", "update"]:
                self.write(self.d / "mozilla.sources", self.SOURCES)  # the conflict appears alongside the new list
                raise subprocess.CalledProcessError(100, cmd, output="E: Conflicting values set for option Signed-By")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        self.patched(run=failing, sudo_write=lambda path, text: self.write(path, text))
        with self.assertRaises(pq.StepError) as raised:
            pq.install_mozilla_repo()
        self.assertIn("Conflicting values set for option Signed-By", str(raised.exception))
        self.assertIn("sudo rm", str(raised.exception))


class Install(Base):
    def run_install(self):
        self.patched(STEPS=[], log_snapshot=lambda: None, open_test_page=lambda: None)
        with self.assertRaises(SystemExit) as raised:
            pq.cmd_install(None)
        return raised.exception.code

    def test_something_waiting_for_you_exits_3_and_is_listed(self):
        self.patched(status_rows=lambda: [("Firefox (Mozilla deb)", True, True)])
        pq.NOTES.append("3. Bluetooth: put a token in $HA_TOKEN")
        self.assertEqual(self.run_install(), 3)
        self.assertIn("waiting for you: 3. Bluetooth: put a token in $HA_TOKEN", self.rec.text)

    def test_an_unmet_required_row_exits_3(self):
        self.patched(status_rows=lambda: [("credentialsd installed", False, True)])
        self.assertEqual(self.run_install(), 3)
        self.assertIn("missing: credentialsd installed", self.rec.text)

    def test_everything_in_place_exits_normally(self):
        self.patched(STEPS=[], log_snapshot=lambda: None, open_test_page=lambda: None,
                     status_rows=lambda: [("Firefox (Mozilla deb)", True, True),
                                          ("Firefox restarted: extension active with access to all sites", False, False)])
        pq.cmd_install(None)  # returns, no SystemExit: a context row does not make the install unfinished
        self.assertIn("Restart Firefox once", self.rec.text)
        self.assertIn("[x] Firefox (Mozilla deb)", self.rec.text)

    def test_dry_run_never_fails_on_state(self):
        self.set_flags(dry_run=True, yes=True)
        self.patched(STEPS=[])
        pq.NOTES.append("3. Bluetooth: put a token in $HA_TOKEN")
        pq.cmd_install(None)
        self.assertIn("Would need you", self.rec.text)


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
        # HA_URL names a Home Assistant without probing the LAN/Tailscale, so the run is hermetic and quick
        env = {**os.environ, "HOME": HOME, "PASSKEY_QR_BLUETOOTH_DIR": BLUETOOTH, "HA_URL": "http://127.0.0.1:9"}
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)

    def test_help_lists_the_new_flags_and_command(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        for word in ("--yes", "--remove-snap", "check"):
            self.assertIn(word, result.stdout)

    def test_status_and_check_leave_no_log_files(self):
        state = Path(HOME) / ".local/state/passkey-qr"
        self.run_cli("status")
        self.run_cli("check")
        self.assertEqual(list(state.glob("*.log")) if state.exists() else [], [])

    def test_dry_run_leaves_no_log_files_and_exits_zero_even_with_nothing_installed(self):
        state = Path(HOME) / ".local/state/passkey-qr"
        result = self.run_cli("--dry-run", "--yes", "install")
        self.assertEqual(result.returncode, 0, result.stdout[-400:] + result.stderr[-400:])
        self.assertEqual(list(state.glob("*.log")) if state.exists() else [], [])

    def test_check_without_bluetooth_is_drift_and_names_it(self):
        clear_bluetooth()
        result = self.run_cli("check")
        self.assertEqual(result.returncode, 3)
        self.assertIn("missing: Bluetooth adapter", result.stdout)


class SelfHealingBase(Base):
    """Everything systemd/D-Bus/ps/journal is faked: `out` answers from a table, `run`/`say` are recorded."""

    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.patched(SYSTEMD_USER=self.root / "systemd", HEALTH_BIN=self.root / "libexec/passkey-qr",
                     HEALTH_STATE=self.root / "state/health.json", STATE_DIR=self.root / "state",
                     REQUEST_MARKER=self.root / "run/passkey-qr/request-pending",
                     PASS_LOCK=self.root / "run/passkey-qr/pass.lock",
                     HA_REPAIR_CONFIG=self.root / "config/passkey-qr/ha-repair", PROBE_RETRY_SECONDS=0)
        self.answers = {"systemctl --user is-enabled": "enabled"}
        self.patched(out=self.fake_out)
        self.notices, self.bodies = [], []
        self.patched(notify=self.record_notice)
        self.real_ssh_docker = pq.ssh_docker

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
                f"Id={name}\nLoadState={'loaded' if name[:-len('.service')] in units else 'not-found'}\n"
                f"Restart={restart if name[:-len('.service')] in units else 'no'}" for name in cmd[9:])
        self.answers["systemctl --user show -p Id"] = answer

    def commands_starting(self, *words):
        return [c for c in self.rec.commands if c[:len(words)] == list(words)]


class RestartPolicy(SelfHealingBase):
    UNITS = ("ha-ble-vhci", "xdg-desktop-portal", "xyz.iinuwa.credentialsd.Credentials",
             "xyz.iinuwa.credentialsd.UiControl", "passwords-appd")

    def test_the_dropin_restarts_forever_without_a_start_limit(self):
        for unit in self.UNITS:
            text = pq.restart_dropin_text(unit)
            self.assertIn("Restart=always\n", text)
            self.assertIn("StartLimitIntervalSec=0\n", text)
            self.assertRegex(text, r"RestartSec=[1-9]\n")
            self.assertLess(text.index("[Unit]"), text.index("StartLimitIntervalSec"))  # a [Unit] setting
            self.assertLess(text.index("StartLimitIntervalSec"), text.index("[Service]"))

    def test_the_whole_chain_is_covered(self):
        self.assertEqual(set(pq.RESTART_SEC), set(self.UNITS))

    def test_units_that_do_not_exist_are_skipped_silently(self):
        self.loaded("ha-ble-vhci")
        pq.write_restart_policy()
        self.assertEqual([p for p, _ in self.rec.files], [pq.restart_dropin("ha-ble-vhci")])
        self.assertEqual(self.commands_starting("systemctl", "--user", "daemon-reload"),
                         [["systemctl", "--user", "daemon-reload"]])

    def test_nothing_installed_means_nothing_to_do(self):
        self.loaded()
        self.assertTrue(pq.restart_policy_ok())

    def test_an_unreachable_user_manager_is_not_mistaken_for_nothing_to_do(self):
        self.answers["systemctl --user show -p Id"] = ""  # `systemctl --user` failed: out() returns ""
        self.assertFalse(pq.restart_policy_ok())
        with self.assertRaises(pq.StepError):
            pq.write_restart_policy()

    def test_dry_run_without_a_user_manager_still_lists_the_step_instead_of_failing(self):
        self.set_flags(dry_run=True)
        self.answers["systemctl --user show -p Id"] = ""
        pq.write_restart_policy()  # only prints: must not raise

    def test_an_aliased_unit_name_does_not_break_the_check(self):
        self.answers["systemctl --user show -p Id"] = "Id=canonical.service\nLoadState=loaded\nRestart=always"
        self.assertTrue(pq.restart_policy_ok())

    def test_the_dropin_sorts_after_any_other_restart_setting(self):
        self.assertTrue(pq.restart_dropin("passwords-appd").name > "90-pc.conf")

    def test_the_check_needs_both_the_file_and_the_loaded_policy(self):
        self.loaded("passwords-appd")
        path = pq.restart_dropin("passwords-appd")
        self.assertFalse(pq.restart_policy_ok())  # file missing
        path.parent.mkdir(parents=True)
        path.write_text(pq.restart_dropin_text("passwords-appd"))
        self.assertTrue(pq.restart_policy_ok())
        self.loaded("passwords-appd", restart="on-failure")  # written but not daemon-reloaded
        self.assertFalse(pq.restart_policy_ok())

    def test_all_units_are_asked_in_one_systemctl_call(self):
        calls = []
        self.patched(out=lambda cmd, timeout=20: calls.append(cmd) or "")
        pq.restart_policy_ok()
        self.assertEqual(len(calls), 1)
        self.assertEqual(len([a for a in calls[0] if a.endswith(".service")]), len(pq.RESTART_SEC))

    def test_a_deliberate_do_not_restart_exit_status_is_never_overridden(self):
        for unit in self.UNITS:
            self.assertNotRegex(pq.restart_dropin_text(unit), r"(?m)^RestartPreventExitStatus=")


class SelfHealingStep(SelfHealingBase):
    def test_it_is_a_required_row_of_check_and_shown_in_status(self):
        self.assertTrue(all(a.required and not a.optional for a in pq.selfheal_actions() if "copy is current" not in a.name))
        copy = next(a for a in pq.selfheal_actions() if "copy is current" in a.name)
        self.assertFalse(copy.required)  # a checkout update makes the copy stale: install refreshes, check stays quiet
        self.assertIn("6. Self-healing", [title for title, _ in pq.status_groups()])
        self.assertIn(pq.step_selfheal, pq.STEPS)

    def test_apply_writes_units_enables_them_and_the_check_then_holds(self):
        self.patched(write_file=lambda path, content, mode=0o644: (path.parent.mkdir(parents=True, exist_ok=True),
                                                                   path.write_text(content), path.chmod(mode)))
        self.patched(SELF=self.root / "passkey-qr")
        pq.SELF.write_text("#!/usr/bin/env python3\n")
        self.loaded("ha-ble-vhci")
        self.old_units()
        self.answers.update({"systemctl --user is-enabled": "enabled", "systemctl --user is-active": "active"})
        self.assertFalse(any(a.check() for a in pq.selfheal_actions()))
        for action in pq.selfheal_actions():
            action.apply()
        self.assertTrue(all(a.check() for a in pq.selfheal_actions()))
        self.assertIn(["systemctl", "--user", "enable", "passkey-qr-watch.service"], self.rec.commands)
        self.assertIn(["systemctl", "--user", "restart", "passkey-qr-watch.service"], self.rec.commands)
        self.assertIn(["systemctl", "--user", "enable", "--now", "passkey-qr-prefs.path"], self.rec.commands)
        service = (pq.SYSTEMD_USER / "passkey-qr-watch.service").read_text()
        self.assertIn(f'ExecStart="{pq.HEALTH_BIN}" watch', service)
        self.assertFalse(list(pq.SYSTEMD_USER.glob("*.timer")))  # not one timer is installed
        self.assertEqual(pq.HEALTH_BIN.stat().st_mode & 0o777, 0o755)

    def old_units(self):
        """What an earlier version installed: the 45 s timer and its service."""
        pq.SYSTEMD_USER.mkdir(parents=True, exist_ok=True)
        for name in pq.OBSOLETE_UNITS:
            (pq.SYSTEMD_USER / name).write_text("[Unit]\nDescription=old\n")

    def test_units_survive_a_home_with_a_space_or_a_percent_sign(self):
        self.patched(HEALTH_BIN=Path("/home/a b/100%/passkey-qr"))
        service = pq.watch_unit_files()[pq.SYSTEMD_USER / "passkey-qr-watch.service"]
        self.assertIn('ExecStart="/home/a b/100%%/passkey-qr" watch', service)

    def test_the_path_unit_watches_both_firefox_profile_lists(self):
        text = pq.prefs_unit_files()[pq.SYSTEMD_USER / "passkey-qr-prefs.path"]
        self.assertIn(f"PathModified={pq.MOZ_PROFILE / 'profiles.ini'}", text)
        self.assertIn(f"PathModified={pq.MOZ_PROFILE / 'installs.ini'}", text)

    def test_a_changed_checkout_makes_the_installed_copy_stale(self):
        self.patched(SELF=self.root / "passkey-qr")
        pq.SELF.write_text("v1")
        pq.HEALTH_BIN.parent.mkdir(parents=True)
        pq.HEALTH_BIN.write_text("v1")
        pq.HEALTH_BIN.chmod(0o755)
        self.assertTrue(pq.health_copy_ok())
        pq.SELF.write_text("v2")
        self.assertFalse(pq.health_copy_ok())

    def test_dry_run_changes_nothing(self):
        self.set_flags(dry_run=True)
        self.loaded("ha-ble-vhci")
        self.old_units()
        for action in pq.selfheal_actions():
            action.apply()
        self.assertFalse(pq.HEALTH_BIN.exists())
        self.assertEqual(sorted(p.name for p in pq.SYSTEMD_USER.iterdir()),
                         ["passkey-qr-health.service", "passkey-qr-health.timer"])  # nothing written, nothing removed

    def test_the_installer_removes_the_45_second_timer_it_used_to_install(self):
        self.old_units()
        self.assertFalse(pq.obsolete_units_gone())
        pq.remove_obsolete_units()
        self.assertTrue(pq.obsolete_units_gone())
        self.assertEqual(self.commands_starting("systemctl", "--user", "disable", "--now"),
                         [["systemctl", "--user", "disable", "--now", "passkey-qr-health.timer"]])

    def test_a_refreshed_copy_restarts_the_running_watcher(self):
        self.patched(SELF=self.root / "passkey-qr")
        pq.SELF.write_text("v2")
        (pq.SYSTEMD_USER).mkdir(parents=True)
        (pq.SYSTEMD_USER / "passkey-qr-watch.service").write_text("[Service]\n")
        pq.install_health_copy()
        self.assertIn(["systemctl", "--user", "try-restart", "passkey-qr-watch.service"], self.rec.commands)

    def test_the_first_install_does_not_try_to_restart_a_watcher_that_does_not_exist_yet(self):
        self.patched(SELF=self.root / "passkey-qr")
        pq.SELF.write_text("v1")
        pq.install_health_copy()
        self.assertEqual(self.commands_starting("systemctl"), [])


class OfflineProxyNotification(SelfHealingBase):
    """`--scanners` exit 4 (no proxy) / 5 (Home Assistant unreachable) while a passkey request is pending ->
    ONE desktop notification per failure; never without a pending request."""

    def setUp(self):
        super().setUp()
        self.patched(scanner_log_says_none=lambda now: False, ha_repair=lambda state, now, down_for: "")

    def pass_with(self, code, now, *, pending=True, reason=""):
        state = pq.load_health_state()
        state["pending"] = pending
        env = {"PASSKEY_QR_HEALTH_FAKE_SCANNERS_EXIT": str(code), "PASSKEY_QR_HEALTH_FAKE_SCANNERS_REASON": reason}
        with mock.patch.dict(os.environ, env):
            pq.health_proxy(state, now)
        pq.save_health_state(state)

    def test_a_missing_proxy_is_told_once_however_often_the_request_is_rechecked(self):
        self.pass_with(4, 1000)
        self.assertEqual(self.notices, [("Bluetooth proxy (ESP32) is offline", True)])
        for tick in range(1, 20):
            self.pass_with(4, 1000 + 20 * tick)
        self.assertEqual(len(self.notices), 1)

    def test_idle_runs_tell_nobody(self):
        for tick in range(10):
            self.pass_with(4, 1000 + 45 * tick, pending=False)
            self.pass_with(5, 1000 + 45 * tick, pending=False)
        self.assertEqual(self.notices, [])
        self.assertIn("bluetooth-proxy: FAILING", self.rec.text)

    def test_recovery_is_announced_exactly_once(self):
        self.pass_with(4, 1000)
        self.pass_with(0, 1020)
        self.pass_with(0, 1040)
        self.assertEqual(self.notices, [("Bluetooth proxy (ESP32) is offline", True),
                                        ("Bluetooth proxy is back online", False)])

    def test_a_proxy_that_was_never_missing_is_never_announced_as_back(self):
        self.pass_with(0, 1000)
        self.assertEqual(self.notices, [])

    def test_home_assistant_down_is_told_differently_from_a_missing_proxy(self):
        self.pass_with(5, 1000)
        self.assertEqual(self.notices, [("Home Assistant is unreachable", True)])

    def test_nothing_is_reported_when_the_bridge_is_not_set_up(self):
        self.patched(scanners_result=lambda: None)
        state = {"pending": True}
        pq.health_proxy(state, 1000)
        self.assertEqual((self.notices, state), ([], {"pending": True}))

    def test_the_real_exit_code_is_used_without_the_override(self):
        self.patched(scanners_result=lambda: subprocess.CompletedProcess([], 4, "", ""))
        pq.health_proxy({"pending": True}, 1000)
        self.assertEqual(len(self.notices), 1)

    def test_the_real_unreachable_exit_code_and_stderr_reach_the_message(self):
        said = "cannot reach Home Assistant at http://x:8123: Cannot connect to host x:8123 [Connect call failed]"
        self.patched(scanners_result=lambda: subprocess.CompletedProcess([], 5, "", said + "\n"))
        pq.health_proxy({"pending": True}, 1000)
        self.assertEqual(self.notices, [("Home Assistant is unreachable", True)])
        self.assertIn("nothing answers on its port", self.bodies[0])

    def test_a_bridge_that_says_none_while_home_assistant_sees_a_scanner_is_restarted_once(self):
        self.patched(scanners_result=lambda: subprocess.CompletedProcess([], 0, "p  source=1\n", ""),
                     scanner_log_says_none=lambda now: True)
        state = {"pending": True}
        pq.health_proxy(state, 1000)
        pq.health_proxy(state, 1020)  # inside the cooldown
        self.assertEqual(self.commands_starting("systemctl", "--user", "restart", "ha-ble-vhci"),
                         [["systemctl", "--user", "restart", "ha-ble-vhci"]])


class JournalPriority(unittest.TestCase):
    def test_the_priority_prefix_is_only_written_when_stdout_really_is_the_journal(self):
        stat = os.fstat(sys.stdout.fileno())
        with mock.patch.dict(os.environ, {"JOURNAL_STREAM": f"{stat.st_dev}:{stat.st_ino}"}):
            self.assertTrue(pq.journal_is_stdout())
        with mock.patch.dict(os.environ, {"JOURNAL_STREAM": "1:2"}):  # inherited from a service: not our stdout
            self.assertFalse(pq.journal_is_stdout())
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("JOURNAL_STREAM", None)
            self.assertFalse(pq.journal_is_stdout())


class DesktopNotification(Base):
    def test_notify_send_gets_a_clear_title_and_a_critical_urgency_for_an_outage(self):
        with mock.patch.object(pq.shutil, "which", return_value="/usr/bin/notify-send"), \
                mock.patch.object(pq.subprocess, "run") as run:
            pq.notify("Bluetooth proxy (ESP32) is offline", "Power it on.", urgent=True)
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "/usr/bin/notify-send")
        self.assertEqual(argv[argv.index("-u") + 1], "critical")
        self.assertEqual(argv[-2:], ["Bluetooth proxy (ESP32) is offline", "Power it on."])
        self.assertEqual(argv[argv.index("-h") + 1], "string:x-canonical-private-synchronous:passkey-qr")
        self.assertIn("notify: Bluetooth proxy (ESP32) is offline", self.rec.text)  # and the journal line

    def test_a_missing_notify_send_is_logged_not_raised(self):
        with mock.patch.object(pq.shutil, "which", return_value=None):
            pq.notify("x", "y")
        self.assertIn("notify-send is not installed", self.rec.text)

    def test_dry_run_sends_nothing(self):
        self.set_flags(dry_run=True)
        with mock.patch.object(pq.subprocess, "run") as run:
            pq.notify("x", "y")
        run.assert_not_called()

    def test_a_failing_notify_send_is_logged_not_mistaken_for_delivered(self):
        failed = subprocess.CompletedProcess([], 1, "", "Failed to connect to socket")
        with mock.patch.object(pq.shutil, "which", return_value="/usr/bin/notify-send"), \
                mock.patch.object(pq.subprocess, "run", return_value=failed):
            pq.notify("x", "y")
        self.assertIn("notify-send failed (exit 1): Failed to connect to socket", self.rec.text)


class StuckRequests(SelfHealingBase):
    PS = ("  100  900 /usr/bin/python3 /usr/local/bin/credentialsd-firefox-helper\n"
          "  101   30 /usr/bin/python3 /usr/local/bin/credentialsd-firefox-helper\n"
          "  102 5000 /usr/lib/firefox/firefox -contentproc\n"
          "  103 5000 vim notes-about-credentialsd-firefox-helper.txt\n")

    def setUp(self):
        super().setUp()
        self.loaded(*pq.CREDENTIALSD_UNITS)
        self.answers["ps -u"] = self.PS
        self.real_still_helper = pq.still_helper
        self.patched(still_helper=lambda pid: True)

    def test_only_helpers_older_than_the_limit_are_found(self):
        self.assertEqual(pq.stuck_helpers(600), [(100, 900)])
        self.assertEqual(pq.stuck_helpers(10), [(100, 900), (101, 30)])

    def test_a_stuck_helper_is_ended_by_pid_and_credentialsd_and_its_ui_are_restarted(self):
        self.answers["ps -u"] = "  100  900 /usr/bin/python3 /usr/local/bin/credentialsd-firefox-helper\n"
        with mock.patch.object(pq.os, "kill") as sigkill:
            pq.health_stuck_requests({}, 0)
        sigkill.assert_called_once_with(100, pq.signal.SIGKILL)
        self.assertEqual(self.commands_starting("systemctl", "--user", "restart"),
                         [["systemctl", "--user", "restart", unit] for unit in pq.CREDENTIALSD_UNITS])

    def test_a_request_in_flight_is_not_aborted_by_the_restart_which_waits_until_it_ends(self):
        state = {}
        with mock.patch.object(pq.os, "kill") as sigkill:
            pq.health_stuck_requests(state, 0)  # PS: helper 100 is stuck, helper 101 (30 s old) is a live request
        sigkill.assert_called_once_with(100, pq.signal.SIGKILL)
        self.assertEqual(self.commands_starting("systemctl", "--user", "restart"), [])
        self.answers["ps -u"] = "  101   60 /usr/bin/python3 /usr/local/bin/credentialsd-firefox-helper\n"
        pq.health_stuck_requests(state, 45)
        self.assertEqual(self.commands_starting("systemctl", "--user", "restart"), [])  # still busy
        self.answers["ps -u"] = ""
        pq.health_stuck_requests(state, 90)  # the request ended: now it is safe
        self.assertEqual(len(self.commands_starting("systemctl", "--user", "restart")), 2)
        pq.health_stuck_requests(state, 135)
        self.assertEqual(len(self.commands_starting("systemctl", "--user", "restart")), 2)  # once, not forever

    def test_a_restart_that_waited_for_a_younger_request_is_done_when_the_last_request_ends(self):
        with mock.patch.object(pq.os, "kill"):
            pq.save_health_state({"restart_credentialsd": True})
            self.answers["ps -u"] = "  101   60 /usr/bin/python3 /usr/local/bin/credentialsd-firefox-helper\n"
            pq.end_request()  # a request is still alive: not now
            self.assertEqual(self.commands_starting("systemctl", "--user", "restart"), [])
            self.assertTrue(pq.load_health_state()["restart_credentialsd"])
            self.answers["ps -u"] = ""
            pq.end_request()  # the last one is gone
        self.assertEqual(len(self.commands_starting("systemctl", "--user", "restart")), 2)
        self.assertFalse(pq.load_health_state()["restart_credentialsd"])

    def test_a_request_that_ends_without_a_stuck_one_restarts_nothing(self):
        self.answers["ps -u"] = ""
        pq.end_request()
        self.assertEqual(self.commands_starting("systemctl", "--user", "restart"), [])

    def test_a_recycled_pid_is_never_ended(self):
        self.patched(still_helper=lambda pid: False)  # the helper exited and another process got its pid
        with mock.patch.object(pq.os, "kill") as sigkill:
            pq.health_stuck_requests({}, 0)
        sigkill.assert_not_called()

    def test_still_helper_reads_the_live_command_line(self):
        self.patched(still_helper=self.real_still_helper)
        self.assertFalse(pq.still_helper(2 ** 22 + 1))  # no such process
        self.assertFalse(pq.still_helper(os.getpid()))  # a live process that is not the helper
        cmdline = b"python3\x00/usr/local/bin/credentialsd-firefox-helper\x00"
        with mock.patch.object(Path, "read_bytes", return_value=cmdline):
            self.assertTrue(pq.still_helper(1234))

    def test_a_helper_started_through_env_is_found(self):
        self.answers["ps -u"] = "  300 5000 /usr/bin/env python3 /usr/local/bin/credentialsd-firefox-helper\n"
        self.assertEqual(pq.stuck_helpers(600), [(300, 5000)])

    def test_tools_that_merely_mention_the_helper_are_not_the_helper(self):
        self.answers["ps -u"] = ("  200 5000 grep credentialsd-firefox-helper\n"
                                 "  201 5000 gdb /usr/local/bin/credentialsd-firefox-helper\n"
                                 "  202 5000 /usr/local/bin/credentialsd-firefox-helper\n")
        self.assertEqual(pq.stuck_helpers(600), [(202, 5000)])

    def test_a_young_request_is_left_alone(self):
        self.answers["ps -u"] = "  101   30 /usr/bin/python3 /usr/local/bin/credentialsd-firefox-helper\n"
        with mock.patch.object(pq.os, "kill") as sigkill:
            pq.health_stuck_requests({}, 0)
        sigkill.assert_not_called()
        self.assertEqual(self.rec.commands, [])

    def test_the_threshold_can_be_lowered_for_a_demo(self):
        with mock.patch.dict(os.environ, {"PASSKEY_QR_STUCK_SECONDS": "10"}), \
                mock.patch.object(pq.os, "kill") as sigkill:
            pq.health_stuck_requests({}, 0)
        self.assertEqual([c.args[0] for c in sigkill.call_args_list], [100, 101])

    def test_a_helper_that_vanished_meanwhile_is_not_an_error(self):
        with mock.patch.object(pq.os, "kill", side_effect=ProcessLookupError):
            pq.health_stuck_requests({}, 0)

    def test_dry_run_ends_nothing(self):
        self.set_flags(dry_run=True)
        with mock.patch.object(pq.os, "kill") as sigkill:
            pq.health_stuck_requests({}, 0)
        sigkill.assert_not_called()


class HealthRepairs(SelfHealingBase):
    def test_a_portal_without_the_credential_interface_is_restarted(self):
        ready = iter([False, True])
        self.patched(portal_credential_ready=lambda: next(ready))
        pq.health_portal({}, 1000)
        self.assertEqual(self.commands_starting("systemctl", "--user", "restart"),
                         [["systemctl", "--user", "restart", "xdg-desktop-portal"]])
        self.assertEqual(self.notices, [])

    def test_a_portal_that_stays_broken_is_restarted_once_per_cooldown_and_told_once(self):
        self.patched(portal_credential_ready=lambda: False)
        state = {"pending": True}
        with mock.patch.object(pq.time, "sleep"), \
                mock.patch.object(pq.time, "monotonic", side_effect=iter(range(0, 1000, 21))):
            pq.health_portal(state, 1000)
            pq.health_portal(state, 1045)
            pq.health_portal(state, 1090)
        self.assertEqual(len(self.commands_starting("systemctl", "--user", "restart", "xdg-desktop-portal")), 1)
        self.assertEqual(self.notices, [("The credential portal is not working", True)])

    def test_a_portal_that_never_gets_the_interface_is_left_alone_after_a_few_restarts(self):
        self.patched(portal_credential_ready=lambda: False)
        state = {}
        with mock.patch.object(pq.time, "sleep"), \
                mock.patch.object(pq.time, "monotonic", side_effect=iter(range(0, 100000, 21))):
            for tick in range(10):
                pq.health_portal(state, 1000 + tick * (pq.RESTART_COOLDOWN + 1))
        self.assertEqual(len(self.commands_starting("systemctl", "--user", "restart", "xdg-desktop-portal")),
                         pq.MAX_PORTAL_RESTARTS)

    def test_a_clock_set_back_does_not_block_repairs(self):
        state = {"acted": {"restart-portal": 5000}}
        self.assertTrue(pq.may_act(state, "restart-portal", 1000, pq.RESTART_COOLDOWN))

    def test_a_healthy_portal_is_left_alone(self):
        self.patched(portal_credential_ready=lambda: True)
        pq.health_portal({}, 0)
        self.assertEqual(self.rec.commands, [])

    def test_credentialsd_and_its_ui_are_started_on_demand_and_restarted_when_that_fails(self):
        self.loaded(*pq.CREDENTIALSD_UNITS)
        answers = iter(["", "u 1", "u 2"])  # Credentials fails once then starts; UiControl is up
        self.answers["busctl --user call org.freedesktop.DBus"] = lambda cmd: next(answers)
        pq.health_credentialsd({}, 1000)
        self.assertEqual(self.commands_starting("systemctl", "--user", "restart"),
                         [["systemctl", "--user", "restart", "xyz.iinuwa.credentialsd.Credentials"]])

    def test_a_stopped_bridge_service_is_started(self):
        self.loaded("ha-ble-vhci")
        self.patched(real_adapters=lambda: [], bt_adapters=lambda: [], scanners_result=lambda: None)
        self.answers["systemctl --user is-active ha-ble-vhci"] = "inactive"
        pq.health_bluetooth({}, 1000)
        self.assertIn(["systemctl", "--user", "restart", "ha-ble-vhci"], self.rec.commands)

    def test_an_unpowered_adapter_is_powered_on(self):
        self.patched(real_adapters=lambda: ["hci0"], bt_adapters=lambda: ["hci0"])
        shown = iter(["Powered: no", "Powered: yes"])
        self.out_calls = []
        self.answers["bluetoothctl power on"] = lambda cmd: self.out_calls.append(cmd) or ""
        self.answers["bluetoothctl show"] = lambda cmd: next(shown)
        pq.health_bluetooth({}, 0)
        self.assertIn(["bluetoothctl", "power", "on"], self.out_calls)  # bounded by a timeout, not via run()
        self.assertEqual(self.notices, [])

    def test_a_bridge_without_an_adapter_gets_one_restart_per_cooldown(self):
        self.loaded("ha-ble-vhci")
        self.patched(real_adapters=lambda: [], bt_adapters=lambda: [], scanners_result=lambda: None,
                     unit_age=lambda unit: 500)
        self.answers["systemctl --user is-active ha-ble-vhci"] = "active"
        with mock.patch.object(pq.time, "sleep"):
            state = {}
            pq.health_bluetooth(state, 1000)
            pq.health_bluetooth(state, 1045)
        self.assertEqual(len(self.commands_starting("systemctl", "--user", "restart", "ha-ble-vhci")), 1)

    def test_a_bridge_the_user_disabled_is_left_alone(self):
        self.loaded("ha-ble-vhci")
        self.answers["systemctl --user is-enabled"] = "disabled"
        self.answers["systemctl --user is-active ha-ble-vhci"] = "inactive"
        self.patched(real_adapters=lambda: [], bt_adapters=lambda: [], scanners_result=lambda: None)
        pq.health_bluetooth({}, 1000)
        self.assertEqual((self.rec.commands, self.notices), ([], []))

    def test_a_stopped_bridge_is_started_at_most_once_per_cooldown(self):
        self.loaded("ha-ble-vhci")
        self.answers["systemctl --user is-active ha-ble-vhci"] = "activating"  # systemd's own Restart= is pending
        self.patched(real_adapters=lambda: [], bt_adapters=lambda: [], scanners_result=lambda: None)
        state = {}
        for tick in range(4):
            pq.health_bluetooth(state, 1000 + 45 * tick)
        self.assertEqual(len(self.commands_starting("systemctl", "--user", "restart", "ha-ble-vhci")), 1)

    def test_a_crashing_bridge_is_not_blamed_on_home_assistant(self):
        crashed = subprocess.CompletedProcess([], 1, "", "Traceback (most recent call last):\n  File x\nImportError: y")
        self.patched(scanners_result=lambda: crashed, scanner_log_says_none=lambda now: False)
        state = {}
        for now in (1000, 1045, 1090):
            pq.health_proxy(state, now)
        self.assertEqual(self.notices, [])

    def test_a_graphical_session_without_the_systemd_target_is_still_a_session(self):
        self.answers["systemctl --user is-active graphical-session.target"] = "inactive"
        self.answers["systemctl --user show-environment"] = "LANG=C\nDISPLAY=:0\n"
        self.assertTrue(pq.session_ready())
        self.answers["systemctl --user show-environment"] = "LANG=C\n"
        self.assertFalse(pq.session_ready())

    def test_a_bridge_that_has_only_just_started_is_not_reported_as_missing_its_adapter(self):
        self.loaded("ha-ble-vhci")
        self.patched(real_adapters=lambda: [], bt_adapters=lambda: [], scanners_result=lambda: None,
                     unit_age=lambda unit: 20)
        self.answers["systemctl --user is-active ha-ble-vhci"] = "active"
        state = {}
        for tick in range(4):
            pq.health_bluetooth(state, 1000 + 45 * tick)
        self.assertEqual((self.notices, self.commands_starting("systemctl", "--user", "restart")), ([], []))

    def test_a_bridge_that_never_registers_an_adapter_is_reported_once_settled(self):
        self.loaded("ha-ble-vhci")
        self.patched(real_adapters=lambda: [], bt_adapters=lambda: [], scanners_result=lambda: None,
                     unit_age=lambda unit: 500)
        self.answers["systemctl --user is-active ha-ble-vhci"] = "active"
        state = {"pending": True}
        with mock.patch.object(pq.time, "sleep"):
            for now in (1000, 1045, 1090):  # the first run restarts the bridge and gives it time
                pq.health_bluetooth(state, now)
        self.assertEqual(self.notices, [("No Bluetooth adapter", True)])

    def test_a_mistyped_stuck_limit_falls_back_to_the_default(self):
        with mock.patch.dict(os.environ, {"PASSKEY_QR_STUCK_SECONDS": "10min"}):
            self.assertEqual(pq.stuck_limit(), pq.STUCK_SECONDS)

    def test_the_state_is_saved_after_every_check_so_a_cut_off_run_keeps_it(self):
        saves = []
        self.patched(session_ready=lambda: False, health_stuck_requests=lambda s, n: s.setdefault("a", 1),
                     health_bluetooth=lambda s, n: saves.append(dict(pq.load_health_state())))
        pq.health(1000)
        self.assertEqual(saves, [{"a": 1, "pending": True}])  # the first check's result was on disk before the second ran

    def test_one_crashing_check_does_not_stop_the_others(self):
        ran = []

        def boom(state, now):
            raise RuntimeError("boom")

        self.patched(session_ready=lambda: False, health_bluetooth=boom,
                     health_stuck_requests=lambda state, now: ran.append("stuck"))
        self.assertEqual(pq.health(1000), 1)
        self.assertEqual(ran, ["stuck"])
        self.assertIn("boom crashed", self.rec.text)

    def test_without_a_graphical_session_the_portal_checks_are_skipped(self):
        ran = []
        self.patched(session_ready=lambda: False, health_bluetooth=lambda s, n: None,
                     health_portal=lambda s, n: ran.append("portal"),
                     health_credentialsd=lambda s, n: ran.append("credentialsd"),
                     health_stuck_requests=lambda s, n: None)
        self.assertEqual(pq.health(1000), 0)
        self.assertEqual(ran, [])

    def test_the_state_survives_between_runs(self):
        self.patched(session_ready=lambda: False, health_bluetooth=lambda s, n: s.setdefault("seen", n),
                     health_stuck_requests=lambda s, n: None)
        pq.health(1000)
        self.assertEqual(pq.load_health_state()["seen"], 1000)

    def test_the_checks_run_in_order_when_there_is_a_graphical_session(self):
        ran = []
        self.patched(session_ready=lambda: True, health_bluetooth=lambda s, n: ran.append("bluetooth"),
                     health_portal=lambda s, n: ran.append("portal"),
                     health_credentialsd=lambda s, n: ran.append("credentialsd"),
                     health_stuck_requests=lambda s, n: ran.append("stuck"))
        pq.health(1000)
        self.assertEqual(ran, ["stuck", "portal", "credentialsd", "bluetooth"])  # the one that can wait on the network last

    def test_a_bridge_too_old_for_scanners_is_not_blamed_on_home_assistant(self):
        self.patched(scanners_result=lambda: subprocess.CompletedProcess([], 2, "", "unrecognized arguments"),
                     scanner_log_says_none=lambda now: False)
        state = {}
        for now in (1000, 1045, 1090):
            pq.health_proxy(state, now)
        self.assertEqual(self.notices, [])

    def test_a_real_adapter_clears_an_old_proxy_outage(self):
        self.patched(real_adapters=lambda: ["hci0"], bt_adapters=lambda: ["hci0"])
        self.answers["bluetoothctl show"] = "Powered: yes"
        state = {"issues": {"bluetooth-proxy": {"fails": 5, "notified": 123}}}
        pq.health_bluetooth(state, 1000)
        self.assertNotIn("bluetooth-proxy", state["issues"])

    def test_a_mistyped_fake_exit_code_falls_back_to_the_real_query(self):
        self.patched(scanners_result=lambda: subprocess.CompletedProcess([], 4, "", ""),
                     scanner_log_says_none=lambda now: False)
        with mock.patch.dict(os.environ, {"PASSKEY_QR_HEALTH_FAKE_SCANNERS_EXIT": "4x"}):
            pq.health_proxy({"pending": True}, 1000)
        self.assertEqual(len(self.notices), 1)

    def test_a_healthy_system_asks_each_thing_once(self):
        self.loaded(*pq.CREDENTIALSD_UNITS)
        calls = []
        self.patched(real_adapters=lambda: ["hci0"], bt_adapters=lambda: ["hci0"],
                     portal_credential_ready=lambda: calls.append("portal") or True)
        self.answers["bluetoothctl show"] = lambda cmd: calls.append("bluetoothctl") or "Powered: yes"
        self.answers["busctl --user call"] = lambda cmd: calls.append(cmd[-2]) or "u 2"
        pq.health_bluetooth({}, 0)
        pq.health_portal({}, 0)
        pq.health_credentialsd({}, 0)
        self.assertEqual(sorted(calls), sorted(["bluetoothctl", "portal", *pq.CREDENTIALSD_UNITS]))


class UnitAge(SelfHealingBase):
    def age(self, stamp, monotonic=2000.0):
        self.answers["systemctl --user show -p ActiveEnterTimestampMonotonic"] = stamp
        with mock.patch.object(pq.time, "monotonic", return_value=monotonic):
            return pq.unit_age("ha-ble-vhci")

    def test_microseconds_since_boot_become_seconds_of_age(self):
        self.assertAlmostEqual(self.age("1500000000", monotonic=2000.0), 500.0)

    def test_a_unit_that_never_started_or_an_unreadable_stamp_has_age_zero(self):
        self.assertEqual(self.age("0"), 0.0)
        self.assertEqual(self.age(""), 0.0)
        self.assertEqual(self.age("n/a"), 0.0)


class HealthDryRun(SelfHealingBase):
    def test_dry_run_does_not_persist_state(self):
        self.set_flags(dry_run=True)
        pq.save_health_state({"acted": {"x": 1}})
        self.assertFalse(pq.HEALTH_STATE.exists())


class ScannerJournal(SelfHealingBase):
    def line(self, age, message):
        return f"{1000 - age}.5 host ha-ble-vhci[1]: {message}"

    def says_none(self, text, unit_age=3600.0):
        self.patched(unit_age=lambda unit: unit_age)
        self.answers["journalctl --user -u ha-ble-vhci"] = text
        return pq.scanner_log_says_none(1000)

    def test_a_scanner_reported_offline_for_longer_than_two_minutes_is_stale(self):
        self.assertTrue(self.says_none(self.line(300, "WARNING no Bluetooth scanner is connected to Home Assistant")))

    def test_one_proxy_offline_while_another_works_is_not_stale(self):
        self.assertFalse(self.says_none(self.line(300, "WARNING Bluetooth scanner offline: btproxy-b")))

    def test_a_fresh_no_scanner_line_or_an_online_line_is_not(self):
        self.assertFalse(self.says_none(self.line(30, "WARNING no Bluetooth scanner is connected to Home Assistant")))
        self.assertFalse(self.says_none(self.line(300, "INFO Bluetooth scanner online: btproxy (AA)")))
        self.assertFalse(self.says_none("-- No entries --"))
        self.assertFalse(self.says_none(""))

    def test_only_the_current_run_of_the_service_is_searched(self):
        calls = []
        self.patched(unit_age=lambda unit: 400.0, out=lambda cmd, timeout=20: calls.append(cmd) or "")
        pq.scanner_log_says_none(1000)
        self.assertEqual(calls[0][calls[0].index("--since") + 1], "@600")  # not "-6h": earlier runs say nothing

    def test_a_service_of_unknown_age_is_never_judged(self):
        self.assertFalse(self.says_none(self.line(300, "WARNING no Bluetooth scanner is connected"), unit_age=0.0))


class HealPrefs(unittest.TestCase):
    """Firefox user.js drift: re-applied by `heal-prefs` and announced in the journal (never on the desktop)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.moz = self.root / "firefox"
        (self.moz / "p.default").mkdir(parents=True)
        (self.moz / "profiles.ini").write_text("[Profile0]\nName=p\nIsRelative=1\nPath=p.default\nDefault=1\n")
        self.said, self.notices = [], []
        for name, value in dict(MOZ_PROFILE=self.moz, say=lambda m="": self.said.append(m),
                                notify=lambda t, b, urgent=False: self.notices.append(t)).items():
            patcher = mock.patch.object(pq, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        pq.DRY_RUN = False
        self.userjs = self.moz / "p.default" / "user.js"

    def test_drift_is_reapplied_flagged_and_only_the_managed_block_is_touched(self):
        self.userjs.write_text('user_pref("mine", 1);\n')
        pq.cmd_heal_prefs(None)
        self.assertTrue(pq.usb_token_off_ok())
        self.assertIn('user_pref("mine", 1);', self.userjs.read_text())
        self.assertEqual(self.notices, [])  # nobody is using a passkey right now: journal only
        self.assertTrue(any("drifted" in line for line in self.said))

    def test_prefs_in_place_are_not_touched_and_nobody_is_told(self):
        pq.disable_usb_token()
        before = self.userjs.read_text()
        self.notices.clear()
        pq.cmd_heal_prefs(None)
        self.assertEqual((self.userjs.read_text(), self.notices), (before, []))

    def test_no_profile_yet_is_not_an_error(self):
        shutil.rmtree(self.moz / "p.default")
        pq.cmd_heal_prefs(None)
        self.assertEqual(self.notices, [])


class HealthCommandLine(unittest.TestCase):
    def test_help_lists_the_health_commands(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=60,
                                env={**os.environ, "HOME": HOME})
        for command in ("watch", "health", "heal-prefs"):
            self.assertIn(command, result.stdout)
        self.assertNotIn("45 s", result.stdout)


class RequestOnlyChecks(SelfHealingBase):
    """Nothing runs while nobody uses a passkey: no timer, no Home Assistant query, no notification."""

    def test_the_watcher_unit_is_a_service_without_any_timer(self):
        files = pq.watch_unit_files()
        self.assertEqual([path.name for path in files], ["passkey-qr-watch.service"])
        text = next(iter(files.values()))
        self.assertIn(f'ExecStart="{pq.HEALTH_BIN}" watch\n', text)
        self.assertIn("Restart=always\n", text)
        self.assertIn("StartLimitIntervalSec=0\n", text)
        for forbidden in ("[Timer]", "OnUnitActiveSec", "OnCalendar", "OnBootSec"):
            self.assertNotIn(forbidden, text)

    def test_health_without_a_pending_request_does_nothing_at_all(self):
        called = []
        self.patched(pending_helpers=lambda: [], health=lambda now=None: called.append("health") or 0,
                     scanners_result=lambda: called.append("query"))
        pq.cmd_health(None)
        self.assertEqual((called, self.notices, self.rec.commands), ([], [], []))
        self.assertIn("no passkey request is pending", self.rec.text)

    def test_health_with_a_pending_request_does_one_pass_and_settles_it(self):
        passes = []
        self.patched(pending_helpers=lambda: [(1, 5)], health=lambda now=None: passes.append(1) or 0)
        pq.save_health_state({"pending": True, "issues": {"k": {"open": True, "fails": 2}}})
        with self.assertRaises(SystemExit) as ended:
            pq.cmd_health(None)
        self.assertEqual((passes, ended.exception.code), ([1], 0))
        self.assertFalse(pq.load_health_state()["pending"])

    def test_a_pass_without_a_pending_request_never_asks_home_assistant(self):
        self.loaded("ha-ble-vhci")
        asked = []
        self.patched(real_adapters=lambda: [], bt_adapters=lambda: ["hci0"], unit_age=lambda unit: 500,
                     scanners_result=lambda: asked.append(1))
        self.answers["systemctl --user is-active ha-ble-vhci"] = "active"
        self.answers["bluetoothctl show"] = "Powered: yes"
        pq.health_bluetooth({}, 1000)
        self.assertEqual(asked, [])
        pq.health_bluetooth({"pending": True}, 1000)
        self.assertEqual(asked, [1])

    def test_a_failure_without_a_pending_request_only_reaches_the_journal(self):
        pq.report({}, "k", False, 1000, up=("up", "b"), down=("down", "b"))
        self.assertEqual(self.notices, [])
        self.assertIn("k: FAILING (1x): down", self.rec.text)


class ServeRequest(SelfHealingBase):
    def setUp(self):
        super().setUp()
        self.helpers, self.passes, self.sleeps = [], [], []
        self.patched(pending_helpers=lambda: self.helpers, health=lambda now=None: self.passes.append(1) or 0)

    def test_a_request_without_a_helper_is_checked_exactly_once(self):
        pq.serve_request(sleep=self.sleeps.append)
        self.assertEqual((len(self.passes), self.sleeps), (1, []))

    def test_a_waiting_helper_keeps_the_request_pending_and_is_rechecked_every_interval(self):
        self.helpers = [(10, 5)]

        def sleep(seconds):
            self.sleeps.append(seconds)
            if len(self.sleeps) == 3:
                self.helpers = []  # the request ended during the third wait

        pq.serve_request(sleep=sleep)
        self.assertEqual(self.sleeps, [pq.REQUEST_CHECK_SECONDS] * 3)
        self.assertEqual(len(self.passes), 3)  # now, after wait 1, after wait 2: not after the one it ended in

    def test_a_request_that_ended_while_waiting_is_not_checked_or_announced_again(self):
        self.helpers = [(10, 5)]
        self.sleeps_seen = []

        def sleep(seconds):
            self.sleeps_seen.append(seconds)
            self.helpers = []

        pq.serve_request(sleep=sleep)
        self.assertEqual((len(self.passes), self.notices), (1, []))

    def test_the_marker_exists_exactly_while_the_request_is_served(self):
        seen = []
        self.patched(health=lambda now=None: seen.append(pq.REQUEST_MARKER.exists()) or 0)
        self.assertFalse(pq.REQUEST_MARKER.exists())
        pq.serve_request(sleep=self.sleeps.append)
        self.assertEqual(seen, [True])
        self.assertFalse(pq.REQUEST_MARKER.exists())

    def test_a_crashing_pass_still_clears_the_marker_and_the_pending_flag(self):
        def boom(now=None):
            raise RuntimeError("boom")

        self.patched(health=boom)
        pq.save_health_state({"pending": True})
        with self.assertRaises(RuntimeError):
            pq.serve_request(sleep=self.sleeps.append)
        self.assertFalse(pq.REQUEST_MARKER.exists())
        self.assertFalse(pq.load_health_state()["pending"])

    def test_the_end_of_a_request_settles_what_was_told_but_keeps_the_rate_limit(self):
        pq.save_health_state({"pending": True, "ha_down": {"since": 1, "last": 2},
                              "issues": {"k": {"open": True, "fails": 3, "told": 5}}})
        pq.serve_request(sleep=self.sleeps.append)
        state = pq.load_health_state()
        self.assertEqual(state["issues"]["k"], {"open": False, "fails": 0, "told": 5})
        self.assertNotIn("ha_down", state)
        self.assertFalse(state["pending"])

    def test_status_shows_whether_a_request_is_pending(self):
        pq.show_request_status()
        self.assertIn("none pending", self.rec.text)
        pq.mark_request(True)
        pq.show_request_status()
        self.assertIn("pending since", self.rec.said[-1])

    def test_a_dry_run_writes_no_marker(self):
        self.set_flags(dry_run=True)
        pq.mark_request(True)
        self.assertFalse(pq.REQUEST_MARKER.exists())

    def test_passes_do_not_overlap(self):
        with pq.pass_lock(wait=True) as first, pq.pass_lock(wait=False) as second:
            self.assertEqual((first, second), (True, False))
        with pq.pass_lock(wait=False) as again:
            self.assertTrue(again)  # released with the first holder

    def test_an_unusable_runtime_directory_does_not_stop_the_pass(self):
        self.patched(PASS_LOCK=Path("/proc/nonexistent/pass.lock"))
        with pq.pass_lock(wait=True) as held:
            self.assertTrue(held)

    def test_a_manual_pass_leaves_a_request_the_watcher_is_serving_alone(self):
        self.helpers = [(1, 5)]
        pq.mark_request(True)
        pq.cmd_health(None)
        self.assertEqual(self.passes, [])
        self.assertIn("serving this request already", self.rec.text)

    def test_a_manual_pass_does_not_start_while_another_pass_runs(self):
        self.helpers = [(1, 5)]
        with pq.pass_lock(wait=True):
            pq.cmd_health(None)
        self.assertEqual(self.passes, [])
        self.assertIn("another health pass is running", self.rec.text)


class Watcher(SelfHealingBase):
    PORTAL = "org.freedesktop.portal.Desktop"

    def message(self, member="CreateCredential", at=1000.0, **over):
        data = {"type": "method_call", "destination": self.PORTAL, "interface": pq.REQUEST_INTERFACE,
                "member": member, "timestamp-realtime": int(at * 1e6)}
        data.update(over)
        return json.dumps(data) + "\n"

    def register(self, app="org.mozilla.firefox", at=1000.0):
        return self.message("Register", at, interface=pq.REGISTRY_INTERFACE,
                            payload={"type": "sa{sv}", "data": [app, {}]})

    def run_watch(self, lines, helpers=(), waiting_at_start=False):
        """``waiting_at_start``: a helper is alive at the startup scan; ``helpers``: what later checks (after a
        portal call) see."""
        served, order = [], []
        monitor = mock.Mock(stdout=iter(lines))
        monitor.wait.return_value = 1
        clock = iter([1010.0, 1100.0, 1200.0, 1300.0])

        def helpers_now():
            order.append("scan")
            if order.count("scan") == 1:  # the startup scan
                return [(1, 1)] if waiting_at_start else []
            return list(helpers)

        self.patched(serve_request=lambda: served.append(1), pending_helpers=helpers_now)
        with mock.patch.object(pq.subprocess, "Popen", side_effect=lambda *a, **k: order.append("monitor") or monitor) as popen, \
                mock.patch.object(pq.time, "time", side_effect=lambda: next(clock)), \
                mock.patch.object(pq.time, "sleep") as sleep, \
                self.assertRaises(SystemExit) as ended:
            pq.cmd_watch(None)
        self.order = order
        return served, popen, sleep, ended.exception

    def test_only_calls_to_the_portal_that_start_a_passkey_are_requests(self):
        self.assertEqual(pq.request_call_time(self.message()), 1000.0)
        self.assertEqual(pq.request_call_time(self.message("GetCredential", 5.5)), 5.5)
        self.assertEqual(pq.request_call_time(self.register()), 1000.0)  # the helper registers first
        for other in (self.message("GetClientCapabilities"),
                      self.message(interface="org.freedesktop.DBus.Properties"),
                      self.message(destination="org.example.Other"),  # same interface, some other service
                      self.message(**{"type": "signal"}), self.register("org.gnome.Other"),
                      self.message("Register", interface=pq.REGISTRY_INTERFACE),  # no payload
                      "not json\n", "[1]\n", "Monitoring bus message stream.\n"):
            self.assertIsNone(pq.request_call_time(other), other)

    def test_a_message_without_a_timestamp_counts_as_now(self):
        line = json.dumps({"type": "method_call", "destination": self.PORTAL, "interface": pq.REQUEST_INTERFACE,
                           "member": "GetCredential"})
        with mock.patch.object(pq.time, "time", return_value=42.0):
            self.assertEqual(pq.request_call_time(line), 42.0)

    def test_the_monitor_only_listens_for_the_two_portal_calls_that_start_a_request(self):
        _, popen, _, _ = self.run_watch([])
        argv = popen.call_args.args[0]
        self.assertEqual(argv[:4], ["busctl", "--user", "monitor", "--json=short"])
        rules = [argv[i + 1] for i, word in enumerate(argv) if word == "--match"]
        self.assertEqual(rules, list(pq.REQUEST_MATCHES))
        self.assertTrue(all("type='method_call'" in rule for rule in rules))
        self.assertIn("interface='org.freedesktop.portal.experimental.Credential'", rules[0])
        self.assertIn("interface='org.freedesktop.host.portal.Registry',member='Register',arg0='org.mozilla.firefox'",
                      rules[1])

    def test_the_monitor_is_attached_before_the_startup_scan_so_no_request_can_slip_between(self):
        self.run_watch([])
        self.assertLess(self.order.index("monitor"), self.order.index("scan"))

    def test_an_idle_bus_means_no_work_and_no_waiting(self):
        served, _, sleep, _ = self.run_watch([])
        self.assertEqual((served, self.notices, self.rec.commands), ([], [], []))
        sleep.assert_not_called()

    def test_each_request_is_served_after_a_helper_is_seen_and_noise_is_ignored(self):
        served, _, sleep, _ = self.run_watch(
            [self.message(at=1000), "Monitoring bus message stream.\n", self.message("GetClientCapabilities", at=1050),
             self.message(at=1100.5)], helpers=[(7, 3)])
        self.assertEqual(len(served), 2)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [pq.REQUEST_SETTLE_SECONDS] * 2)

    def test_a_hung_portal_is_noticed_from_the_helpers_registration_alone(self):
        served, _, _, _ = self.run_watch([self.register()], helpers=[(7, 3)])
        self.assertEqual(len(served), 1)

    def test_portal_calls_without_a_firefox_helper_behind_them_are_not_requests(self):
        flood = [self.message(at=1000 + i / 100) for i in range(50)]  # something spamming the portal
        served, _, sleep, _ = self.run_watch(flood, helpers=[])
        self.assertEqual(served, [])
        self.assertEqual(sleep.call_count, 1)  # the first call settled the question for the whole backlog
        self.assertIn("without a Firefox helper behind it", self.rec.text)

    def test_calls_made_while_a_request_was_served_belong_to_it(self):
        served, _, _, _ = self.run_watch([self.message(at=1000), self.message("GetCredential", at=1005),
                                          self.message(at=1100)], helpers=[(7, 3)])
        self.assertEqual(len(served), 2)  # the second line (1005) is older than the end of the first serving (1010)

    def test_a_request_already_waiting_when_the_watcher_starts_is_served(self):
        served, _, _, _ = self.run_watch([], waiting_at_start=True)
        self.assertEqual(len(served), 1)

    def test_a_marker_left_by_a_killed_run_is_cleared_at_start(self):
        pq.mark_request(True)
        self.run_watch([])
        self.assertFalse(pq.REQUEST_MARKER.exists())

    def test_a_dead_monitor_ends_the_watcher_so_systemd_restarts_it(self):
        _, _, _, ended = self.run_watch([])
        self.assertIn("busctl monitor ended (exit 1)", str(ended.code))


class NotificationRules(SelfHealingBase):
    """report(): once per failure, rate limited across requests, 'recovered' only for a request that was told."""

    def failing(self, now, pending=True):
        state = pq.load_health_state()
        state["pending"] = pending
        pq.report(state, "k", False, now, up=("back", "ok"), down=("down", "bad"))
        pq.save_health_state(state)

    def recover(self, now):
        state = pq.load_health_state()
        state["pending"] = True
        pq.report(state, "k", True, now, up=("back", "ok"))
        pq.save_health_state(state)

    def titles(self):
        return [title for title, _ in self.notices]

    def test_a_failure_is_told_once_however_often_it_is_rechecked(self):
        for tick in range(20):
            self.failing(1000 + 20 * tick)
        self.assertEqual(self.titles(), ["down"])

    def test_recovery_is_told_once_to_the_request_that_heard_the_failure(self):
        self.failing(1000)
        self.recover(1020)
        self.recover(1040)
        self.assertEqual(self.titles(), ["down", "back"])

    def test_a_failure_that_ends_with_its_request_is_not_announced_as_recovered_by_the_next_one(self):
        self.failing(1000)
        pq.end_request()
        self.recover(1500)
        self.assertEqual(self.titles(), ["down"])

    def test_a_retrying_user_is_not_told_twice_within_the_rate_limit_but_hears_that_it_is_over(self):
        self.failing(1000)
        pq.end_request()
        self.failing(1060)  # a second request while the same outage lasts
        self.assertEqual(self.titles(), ["down"])
        self.recover(1080)
        self.assertEqual(self.titles(), ["down", "back"])

    def test_a_request_after_the_rate_limit_is_told_again(self):
        self.failing(1000)
        pq.end_request()
        self.failing(1000 + pq.NOTIFY_MIN_SECONDS + 1)
        self.assertEqual(self.titles(), ["down", "down"])

    def test_a_clock_set_back_does_not_silence_the_user_forever(self):
        self.failing(5000)
        pq.end_request()
        self.failing(1000)
        self.assertEqual(self.titles(), ["down", "down"])

    def test_a_failure_that_never_recovers_is_never_called_recovered(self):
        self.failing(1000)
        self.failing(1020)
        self.assertEqual(self.titles(), ["down"])

    def test_a_blip_is_never_announced_when_nothing_was_told(self):
        self.recover(1000)
        self.assertEqual(self.notices, [])


class HomeAssistantReasons(unittest.TestCase):
    def test_what_the_bridge_said_becomes_words(self):
        for said, expected in (
            ("cannot reach Home Assistant at http://odroidn2:8123: Cannot connect to host odroidn2:8123 ssl:default "
             "[Connect call failed ('100.116.57.66', 8123)]", "nothing answers on its port"),
            ("cannot reach Home Assistant at http://x:8123: [Errno -2] Name or service not known", "does not resolve"),
            ("Temporary failure in name resolution", "does not resolve"),
            ("[Errno 101] Network is unreachable", "network is unreachable"),
            ("Home Assistant did not answer the scanner subscription", "no answer in time"),
            ("Home Assistant dropped the connection during the handshake", "dropped the connection"),
            ("Timeout on reading data from socket", "no answer in time"),
        ):
            self.assertIn(expected, pq.ha_reason(said), said)

    def test_an_unknown_complaint_is_passed_through_trimmed_and_an_empty_one_is_named(self):
        self.assertEqual(pq.ha_reason("weird " * 40), ("weird " * 40)[:120])
        self.assertEqual(pq.ha_reason(""), "no answer")

    def test_the_bridges_own_prefix_is_not_repeated_in_the_message(self):
        self.assertEqual(pq.ha_reason("cannot reach Home Assistant at http://x:8123: something odd"), "something odd")
        self.assertIn("dropped the connection",
                      pq.ha_reason("cannot reach Home Assistant at http://127.0.0.1:18123: Server disconnected"))


class HomeAssistantDown(SelfHealingBase):
    """What the user is told, and what is done, when the bridge cannot reach Home Assistant during a request."""

    URL = "http://odroidn2:8123"
    REFUSED = ("cannot reach Home Assistant at http://odroidn2:8123: Cannot connect to host odroidn2:8123 ssl:default "
               "[Connect call failed ('100.116.57.66', 8123)]")

    def setUp(self):
        super().setUp()
        self.patched(scanner_log_says_none=lambda now: False, ha_url_configured=lambda: self.URL)
        self.queries = []

    def answer(self, *codes, reason=REFUSED):
        replies = iter(codes)
        self.patched(scanner_query=lambda: self.queries.append(1) or (next(replies), reason))

    def test_unreachable_is_told_with_the_reason_and_what_happens_next(self):
        self.answer(5, 5)
        state = {"pending": True}
        pq.health_proxy(state, 1000)
        self.assertEqual(self.notices, [("Home Assistant is unreachable", True)])
        body = self.bodies[0]
        self.assertIn(self.URL, body)
        self.assertIn("nothing answers on its port", body)
        self.assertIn("you are told when it is back", body)
        self.assertNotIn("exit 5", body)
        self.assertNotIn("exit 1", body)

    def test_a_failed_query_is_repeated_once_before_the_user_is_told(self):
        self.answer(5, 0)
        pq.health_proxy({"pending": True}, 1000)
        self.assertEqual((self.notices, len(self.queries)), ([], 2))

    def test_a_query_that_is_fine_is_asked_once(self):
        self.answer(0)
        pq.health_proxy({"pending": True}, 1000)
        self.assertEqual(len(self.queries), 1)

    def test_a_missing_proxy_is_not_repeated_and_says_the_esp32_returns_after_a_restart(self):
        self.answer(4)
        pq.health_proxy({"pending": True}, 1000)
        self.assertEqual((self.notices, len(self.queries)), ([("Bluetooth proxy (ESP32) is offline", True)], 1))
        self.assertIn("has just restarted", self.bodies[0])

    def test_a_refused_token_is_not_called_unreachable(self):
        self.answer(1, reason="authentication failed: {'type': 'auth_invalid'}")
        pq.health_proxy({"pending": True}, 1000)
        self.assertEqual(self.notices, [("Home Assistant refuses the passkey bridge", True)])
        self.assertIn("auth_invalid", self.bodies[0])
        self.assertIn("token", self.bodies[0])

    def test_home_assistant_answering_again_ends_the_outage_tracking(self):
        state = {"pending": True, "ha_down": {"since": 1, "last": 2}}
        self.answer(4)
        pq.health_proxy(state, 1000)
        self.assertNotIn("ha_down", state)

    def test_a_bridge_crash_is_never_blamed_on_home_assistant(self):
        self.answer(70)
        pq.health_proxy({"pending": True}, 1000)
        self.assertEqual(self.notices, [])

    def test_a_home_assistant_that_accepts_the_connection_but_never_answers_is_unreachable(self):
        bridge, cfg = self.root / "bridge", self.root / "cfg"
        cfg.mkdir()
        bridge.write_text("#!/bin/sh\n")
        (cfg / "token").write_text("not-a-real-token\n")
        self.patched(BRIDGE_BIN=bridge, BRIDGE_CFG=cfg)
        with mock.patch.object(pq.subprocess, "run", side_effect=subprocess.TimeoutExpired("bridge", 15)) as run:
            pq.health_proxy({"pending": True}, 1000)
        self.assertEqual(run.call_count, 1)  # it already waited its full timeout: not asked a second time
        self.assertEqual(self.notices, [("Home Assistant is unreachable", True)])
        self.assertIn("no answer in time", self.bodies[0])

    def test_an_unconfigured_bridge_is_still_not_an_outage(self):
        self.patched(BRIDGE_BIN=self.root / "missing")
        pq.health_proxy({"pending": True}, 1000)
        self.assertEqual(self.notices, [])

    def test_the_outage_is_timed_across_passes_and_restarts_after_a_gap(self):
        state = {}
        self.assertEqual(pq.note_ha_down(state, 1000), 0)
        self.assertEqual(pq.note_ha_down(state, 1020), 20)
        self.assertEqual(pq.note_ha_down(state, 1100), 100)
        self.assertEqual(pq.note_ha_down(state, 1100 + pq.HA_DOWN_GAP_SECONDS + 1), 0)  # a new outage


class HomeAssistantRepair(SelfHealingBase):
    """Looking at, and when it is really broken restarting, Home Assistant's container over the user's own SSH."""

    NOW = calendar.timegm((2026, 10, 23, 12, 0, 0))
    RUNNING = "running|healthy|2026-10-20T10:00:00.5Z|2026-10-20T09:59:00Z"
    UNHEALTHY = "running|unhealthy|2026-10-20T10:00:00.5Z|2026-10-20T09:59:00Z"
    STOPPED = "exited|none|2026-10-22T10:00:00Z|2026-10-23T08:00:00Z"

    def setUp(self):
        super().setUp()
        pq.HA_REPAIR_CONFIG.parent.mkdir(parents=True)
        pq.HA_REPAIR_CONFIG.write_text("HA_CONTAINER=homeassistant-homeassistant-1\n")
        self.patched(ha_url_configured=lambda: "http://odroidn2:8123", ssh_docker=self.fake_docker)
        self.docker, self.inspect, self.action_code = [], self.RUNNING, 0

    def fake_docker(self, host, *words, timeout=25):
        self.docker.append((host, *words))
        if words[0] == "inspect":
            return subprocess.CompletedProcess([], 0 if self.inspect else 255, self.inspect, "")
        return subprocess.CompletedProcess([], self.action_code, "", "Error: no such container")

    def actions(self):
        return [call for call in self.docker if call[1] != "inspect"]

    def plan(self, down_for=0.0, **fields):
        info = {"status": "running", "health": "healthy", "started": self.NOW - 3600, "finished": 0.0, **fields}
        return pq.ha_container_plan(info, self.NOW, down_for)[0]

    def test_the_decision_table(self):
        self.assertEqual(self.plan(status="exited", finished=self.NOW - 3600), "start")
        self.assertEqual(self.plan(status="exited", finished=self.NOW - 5), "wait")  # Docker restarts it itself
        self.assertEqual(self.plan(status="dead"), "start")
        self.assertEqual(self.plan(status="created"), "start")
        self.assertEqual(self.plan(status="paused"), "unpause")
        self.assertEqual(self.plan(status="restarting"), "wait")
        self.assertEqual(self.plan(health="healthy", down_for=10 * 3600), "wait")  # a healthy container is never restarted
        self.assertEqual(self.plan(health="none", down_for=10 * 3600), "wait")
        self.assertEqual(self.plan(health="unhealthy", down_for=pq.HA_HUNG_SECONDS - 1), "wait")
        self.assertEqual(self.plan(health="unhealthy", down_for=pq.HA_HUNG_SECONDS), "restart")
        self.assertEqual(self.plan(health="unhealthy", down_for=3600, started=self.NOW - 100), "wait")  # still booting

    def test_a_stopped_container_is_started_and_the_user_is_told_so(self):
        self.inspect = self.STOPPED
        said = pq.ha_repair({}, self.NOW, 0)
        self.assertEqual(self.actions(), [("odroidn2", "start", "homeassistant-homeassistant-1")])
        self.assertIn("was stopped: it was started", said)

    def test_a_core_restart_with_the_container_up_is_left_alone(self):
        said = pq.ha_repair({}, self.NOW, 150)
        self.assertEqual(self.actions(), [])
        self.assertEqual(len(self.docker), 1)  # the inspect
        self.assertIn("itself is restarting", said)

    def test_a_deliberate_restart_every_few_minutes_is_never_fought(self):
        self.inspect = self.UNHEALTHY
        state = {}
        for restart in range(3):  # three core restarts, each 3 min of 'unreachable', 4 min apart
            base = self.NOW + restart * 600
            for second in range(0, 181, 20):
                pq.ha_repair(state, base + second, pq.note_ha_down(state, base + second))
        self.assertEqual(self.actions(), [])

    def test_a_container_hung_for_minutes_is_restarted_once(self):
        self.inspect = self.UNHEALTHY
        state = {}
        for second in range(0, 600, 20):
            pq.ha_repair(state, self.NOW + second, pq.note_ha_down(state, self.NOW + second))
        self.assertEqual(self.actions(), [("odroidn2", "restart", "homeassistant-homeassistant-1")])

    def test_it_is_looked_at_over_ssh_at_most_once_a_minute(self):
        state = {}
        pq.ha_repair(state, self.NOW, 0)
        self.assertEqual(pq.ha_repair(state, self.NOW + 20, 20), "")
        self.assertEqual(len(self.docker), 1)
        pq.ha_repair(state, self.NOW + 61, 61)
        self.assertEqual(len(self.docker), 2)

    def test_a_repair_is_not_repeated_within_the_cooldown(self):
        self.inspect = self.STOPPED
        state = {}
        pq.ha_repair(state, self.NOW, 0)
        said = pq.ha_repair(state, self.NOW + 120, 120)  # still stopped two minutes later: Docker/ssh did not help
        self.assertEqual(len(self.actions()), 1)
        self.assertIn("was stopped", said)

    def test_without_ssh_access_nothing_is_done_and_the_user_is_told_why(self):
        self.inspect = ""
        said = pq.ha_repair({}, self.NOW, 0)
        self.assertEqual(self.actions(), [])
        self.assertIn("over SSH", said)

    def test_a_failed_repair_is_reported_not_claimed(self):
        self.inspect, self.action_code = self.STOPPED, 1
        said = pq.ha_repair({}, self.NOW, 0)
        self.assertIn("did not work", said)
        self.assertNotIn("it was started", said)

    def test_without_a_configured_container_nothing_is_touched(self):
        pq.HA_REPAIR_CONFIG.unlink()
        self.assertEqual(pq.ha_repair({}, self.NOW, 9999), "")
        self.assertEqual(self.docker, [])

    def test_a_dry_run_looks_but_never_acts(self):
        self.set_flags(dry_run=True)
        self.inspect = self.STOPPED
        pq.ha_repair({}, self.NOW, 0)
        self.assertEqual(self.actions(), [])

    def test_the_ssh_host_defaults_to_the_home_assistant_host_and_can_be_set(self):
        self.assertEqual(pq.ha_repair_target(), ("odroidn2", "homeassistant-homeassistant-1"))
        pq.HA_REPAIR_CONFIG.write_text("HA_CONTAINER=ha\nHA_SSH_HOST=me@box\n")
        self.assertEqual(pq.ha_repair_target(), ("me@box", "ha"))

    def test_values_that_could_smuggle_a_command_or_an_option_are_refused(self):
        for bad in ("HA_CONTAINER=x;reboot", "HA_CONTAINER=$(id)", "HA_CONTAINER=a|b", "HA_CONTAINER=-rf"):
            pq.HA_REPAIR_CONFIG.write_text(bad + "\n")
            self.assertIsNone(pq.ha_repair_target(), bad)
        pq.HA_REPAIR_CONFIG.write_text("HA_CONTAINER=ha\nHA_SSH_HOST=-oProxyCommand=evil\n")
        self.assertIsNone(pq.ha_repair_target())

    def test_a_missing_or_url_less_config_means_no_repair(self):
        self.patched(ha_url_configured=lambda: None)
        pq.HA_REPAIR_CONFIG.write_text("HA_CONTAINER=ha\n")
        self.assertIsNone(pq.ha_repair_target())
        pq.HA_REPAIR_CONFIG.unlink()
        self.assertIsNone(pq.ha_repair_target())

    def test_docker_runs_over_batchmode_ssh_with_every_word_quoted(self):
        self.patched(ssh_docker=self.real_ssh_docker)
        with mock.patch.object(pq.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            pq.ssh_docker("odroidn2", "inspect", "--format", "{{.State.Status}} x", "name")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:6], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "odroidn2"])
        self.assertEqual(argv[6], "docker inspect --format '{{.State.Status}} x' name")

    def test_ssh_that_cannot_run_is_none_not_a_crash(self):
        self.patched(ssh_docker=self.real_ssh_docker)
        with mock.patch.object(pq.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 25)):
            self.assertIsNone(pq.ssh_docker("h", "ps"))
        with mock.patch.object(pq.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(pq.ssh_docker("h", "ps"))

    def test_docker_timestamps(self):
        self.assertAlmostEqual(pq.docker_time("2026-10-23T01:03:34.99516253Z"),
                               calendar.timegm((2026, 10, 23, 1, 3, 34)) + 0.99516253)
        self.assertEqual(pq.docker_time("2026-10-23T01:03:34Z"), calendar.timegm((2026, 10, 23, 1, 3, 34)))
        self.assertEqual(pq.docker_time("0001-01-01T00:00:00Z"), 0.0)
        self.assertEqual(pq.docker_time(""), 0.0)

    def test_the_notification_carries_what_was_found_and_done(self):
        self.inspect = self.STOPPED
        self.patched(scanner_log_says_none=lambda now: False, scanner_query=lambda: (5, "Connect call failed"))
        pq.health_proxy({"pending": True}, self.NOW)
        self.assertIn("Its container was stopped: it was started", self.bodies[0])
        self.assertEqual(self.notices, [("Home Assistant is unreachable", True)])


if __name__ == "__main__":
    unittest.main()
