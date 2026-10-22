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


if __name__ == "__main__":
    unittest.main()
