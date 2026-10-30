"""passkey-qr, step 2: Firefox from the Mozilla repo, the snap profile copy and the profile Firefox starts with."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import Base, cli, core, extension, firefox, patch_named

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


class FirefoxStep(Base):
    def test_a_running_firefox_makes_yes_wait_instead_of_prompting(self):
        self.set_flags(yes=True)
        self.patched(snap_profile=lambda: Path(tempfile.mkdtemp()), firefox_running=lambda: True)
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")), self.assertRaises(core.NeedsYou):
            firefox.copy_profile()
        self.assertEqual(self.rec.commands, [])

    def test_switching_the_start_profile_also_waits_for_a_closed_firefox(self):
        self.set_flags(yes=True)
        self.patched(migrated_name=lambda: "x.default", firefox_running=lambda: True)
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")), self.assertRaises(core.NeedsYou):
            firefox.set_install_default()

    def test_yes_never_closes_or_starts_firefox(self):
        self.set_flags(yes=True)
        with mock.patch("subprocess.Popen", side_effect=AssertionError("started Firefox")):
            extension.restart_firefox()
        self.assertEqual(self.rec.commands, [])
        self.assertIn("not restarted by --yes", self.rec.text)

    def test_a_dry_run_with_yes_previews_exactly_what_yes_does_to_firefox(self):
        self.set_flags(yes=True, dry_run=True)
        self.patched(firefox_running=lambda: True)
        extension.restart_firefox()
        self.assertEqual(self.rec.commands, [])  # no pkill, no launch in the preview either
        self.assertIn("not restarted by --yes", self.rec.text)

    def test_an_interactive_dry_run_with_firefox_open_does_not_wait_for_it_to_close(self):
        self.set_flags(dry_run=True)
        calls = []
        self.patched(firefox_running=lambda: calls.append(1) or True)
        with mock.patch("time.sleep", side_effect=AssertionError("waited")):
            extension.restart_firefox()
        self.assertIn(["pkill", "-TERM", "-x", "firefox"], self.rec.commands)
        self.assertEqual(self.rec.commands[-1], [core.FIREFOX_BIN])

    def test_yes_never_opens_the_test_page(self):
        self.set_flags(yes=True)
        with mock.patch("subprocess.Popen", side_effect=AssertionError("opened a tab")):
            cli.open_test_page()

    def test_the_restart_row_is_context_not_a_requirement(self):
        row = [a for a in extension.extension_actions() if a.name.startswith("Firefox restarted")]
        self.assertEqual([a.required for a in row], [False])

    def test_an_entry_managed_by_ubuntu_tweaks_counts_as_the_policy(self):
        entry = {"installation_mode": "force_installed", "install_url": extension.XPI_URL}
        self.patched(policy_data=lambda: {"policies": {"ExtensionSettings": {core.XPI_ID: entry}}})
        self.assertTrue(extension.policy_ok())
        self.patched(policy_data=lambda: {"policies": {"ExtensionSettings": {core.XPI_ID: {**entry, "install_url": "x"}}}})
        self.assertFalse(extension.policy_ok())

    def snap_action(self):
        state = {"present": True}

        def remove():
            state["present"] = False
            self.rec.run(["snap", "remove", "firefox"], sudo=True)

        return core.Action("Firefox snap removed", lambda: not state["present"], remove, optional=True, destructive=True)

    def test_yes_keeps_the_snap_without_remove_snap(self):
        self.set_flags(yes=True)
        core.run_actions("2. Firefox", [self.snap_action()])
        self.assertEqual(self.rec.commands, [])
        self.assertIn("skipped: Firefox snap removed", self.rec.text)

    def test_yes_with_remove_snap_removes_it(self):
        self.set_flags(yes=True, remove_snap=True)
        core.run_actions("2. Firefox", [self.snap_action()])
        self.assertEqual(self.rec.commands, [["sudo", "snap", "remove", "firefox"]])

    def test_the_firefox_snap_action_is_marked_destructive(self):
        snap = [a for a in firefox.firefox_actions() if "snap removed" in a.name]
        self.assertEqual([(a.optional, a.destructive) for a in snap], [(True, True)])


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
            patcher = patch_named(name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in dict(firefox_running=lambda: False, say=lambda *a, **k: None).items():
            patcher = patch_named(name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        core.DRY_RUN = core.ASSUME_YES = False
        self.addCleanup(lambda: setattr(core, "ASSUME_YES", False))
        self.addCleanup(lambda: setattr(core, "DRY_RUN", False))

    def write(self, name, text):
        (self.moz / name).write_text(text)

    def note_migration(self, name="snap.default"):
        """What copy_profile leaves behind: the evidence that this profile came from the snap."""
        firefox.MIGRATED_NOTE.parent.mkdir(parents=True, exist_ok=True)
        firefox.MIGRATED_NOTE.write_text(name + "\n")

    def test_the_switch_is_pending_on_the_first_run_before_the_profile_was_copied(self):
        # step 2 computes its pending actions up front: the copy does not exist yet, the snap profile does
        snap = self.root / "snap-fx"
        (snap / "snap.default").mkdir(parents=True)
        (snap / "snap.default" / "prefs.js").write_text("//")
        (snap / "profiles.ini").write_text(SNAP_INI)
        with mock.patch.object(firefox, "SNAP_PROFILE", snap):
            shutil.rmtree(self.moz / "snap.default")  # not copied yet
            self.assertEqual(firefox.migrated_name(), "snap.default")
            self.assertFalse(firefox.migrated_is_default())

    def test_a_dry_run_with_yes_shows_a_running_firefox_as_waiting(self):
        core.DRY_RUN = core.ASSUME_YES = True
        with mock.patch.object(firefox, "firefox_running", lambda: True), self.assertRaises(core.NeedsYou):
            firefox.need_firefox_closed()

    def test_a_machine_without_firefox_profiles_has_nothing_to_switch(self):
        self.assertIsNone(firefox.install_default_name())
        self.assertTrue(firefox.migrated_is_default())

    def test_before_the_debs_first_start_the_install_is_seeded_so_it_adopts_the_migrated_profile(self):
        # the copied snap profiles.ini has the snap's own [Install<hash>] (its hash is not the deb's) and no
        # installs.ini exists yet: the deb would create a fresh profile on its first start
        self.write("profiles.ini", DEB_INI.replace(
            "[Install4F96D1932A9F858E]\nDefault=fresh.default-release", "[Install1234567890ABCDEF]\nDefault=snap.default"))
        self.note_migration()
        self.assertIsNone(firefox.install_default_name())  # the snap's section says nothing about the deb
        self.assertFalse(firefox.migrated_is_default())
        firefox.set_install_default()
        self.assertEqual(firefox.install_default_name(), "snap.default")
        self.assertTrue(firefox.migrated_is_default())
        installs = (self.moz / "installs.ini").read_text()
        self.assertIn("[4F96D1932A9F858E]", installs)
        self.assertIn("Locked=1", installs)
        self.assertIn("[Install1234567890ABCDEF]", (self.moz / "profiles.ini").read_text())  # untouched

    def test_a_machine_that_never_started_firefox_gets_a_complete_profiles_ini(self):
        (self.moz / "profiles.ini").unlink(missing_ok=True)
        self.note_migration()
        firefox.set_install_default()
        text = (self.moz / "profiles.ini").read_text()
        self.assertIn("StartWithLastProfile=1", text)
        self.assertIn("Path=snap.default", text)
        self.assertEqual(firefox.install_default_name(), "snap.default")

    def test_the_install_default_is_the_profile_firefox_starts(self):
        self.write("profiles.ini", DEB_INI)
        self.write("installs.ini", INSTALLS_INI)
        self.note_migration()
        self.assertEqual(firefox.install_default_name(), "fresh.default-release")
        self.assertEqual(firefox.active_profile(), self.moz / "fresh.default-release")
        self.assertEqual(firefox.migrated_name(), "snap.default")  # the copy, found without the snap's own directory
        self.assertFalse(firefox.migrated_is_default())

    def test_switching_points_both_files_at_the_copy_and_keeps_backups_and_the_fresh_profile(self):
        self.write("profiles.ini", DEB_INI)
        self.write("installs.ini", INSTALLS_INI)
        self.note_migration()
        firefox.set_install_default()
        self.assertEqual(firefox.install_default_name(), "snap.default")
        self.assertTrue(firefox.migrated_is_default())
        self.assertIn("Default=snap.default", (self.moz / "installs.ini").read_text())
        self.assertIn("Default=fresh.default-release", (self.moz / "installs.ini.bak-passkey-qr").read_text())
        self.assertTrue((self.moz / "profiles.ini.bak-passkey-qr").exists())
        self.assertTrue((self.moz / "fresh.default-release").is_dir())  # nothing is deleted
        again = (self.moz / "profiles.ini").read_text()
        firefox.set_install_default()  # idempotent
        self.assertEqual((self.moz / "profiles.ini").read_text(), again)

    def test_a_profile_missing_from_profiles_ini_gets_a_section(self):
        self.write("profiles.ini", "[General]\nVersion=2\n\n[Install4F96D1932A9F858E]\nDefault=fresh.default-release\n")
        self.write("installs.ini", INSTALLS_INI)
        self.note_migration()
        firefox.set_install_default()
        self.assertEqual(firefox.install_default_name(), "snap.default")
        self.assertIn("Path=snap.default", (self.moz / "profiles.ini").read_text())

    def test_a_running_firefox_blocks_the_switch_before_anything_is_written(self):
        self.write("profiles.ini", DEB_INI)
        self.write("installs.ini", INSTALLS_INI)
        self.note_migration()
        core.ASSUME_YES = True
        with mock.patch.object(firefox, "firefox_running", lambda: True), self.assertRaises(core.NeedsYou):
            firefox.set_install_default()
        self.assertEqual((self.moz / "profiles.ini").read_text(), DEB_INI)
        self.assertFalse((self.moz / "profiles.ini.bak-passkey-qr").exists())


    def test_the_snaps_own_install_section_is_not_a_second_install(self):
        snap_copy = DEB_INI.replace("[Profile1]", "[Install1234567890ABCDEF]\nDefault=snap.default\nLocked=1\n\n[Profile1]")
        self.write("profiles.ini", snap_copy)
        self.write("installs.ini", INSTALLS_INI)
        self.note_migration()
        core.ASSUME_YES = True
        firefox.set_install_default()  # must not wait for a person
        self.assertEqual(firefox.install_default_name(), "snap.default")

    def test_a_legacy_default_profile_of_a_stock_firefox_is_not_a_migrated_one(self):
        self.write("profiles.ini", DEB_INI)  # Default=1 sits on snap.default, as in any Firefox upgraded from pre-67
        self.write("installs.ini", INSTALLS_INI)
        self.assertIsNone(firefox.migrated_name())  # no note, no snap profile: no evidence of a migration
        self.assertTrue(firefox.migrated_is_default())  # so there is nothing to switch

    def test_the_switch_refuses_a_profile_that_was_never_copied(self):
        self.write("profiles.ini", DEB_INI)
        self.write("installs.ini", INSTALLS_INI)
        self.note_migration("not-copied.default")  # the note names a directory that is not there
        with mock.patch.object(firefox, "migrated_name", lambda: "not-copied.default"), self.assertRaises(core.StepError):
            firefox.set_install_default()
        self.assertEqual((self.moz / "installs.ini").read_text(), INSTALLS_INI)
        self.assertFalse(firefox.SWITCHED_NOTE.exists())

    def test_a_later_choice_of_the_user_is_not_drift_and_is_not_reverted(self):
        self.write("profiles.ini", DEB_INI)
        self.write("installs.ini", INSTALLS_INI)
        self.note_migration()
        firefox.set_install_default()
        (self.moz / "installs.ini").write_text(INSTALLS_INI)  # the user picks the fresh profile again
        self.assertEqual(firefox.install_default_name(), "fresh.default-release")
        self.assertTrue(firefox.migrated_is_default())  # switched once; their choice stands

    def test_the_first_backup_is_the_users_original_and_is_never_overwritten(self):
        self.write("profiles.ini", DEB_INI)
        self.write("installs.ini", INSTALLS_INI)
        self.note_migration()
        firefox.set_install_default()
        # something moves the default again, then the installer runs once more
        (self.moz / "installs.ini").write_text(INSTALLS_INI.replace("fresh.default-release", "elsewhere"))
        firefox.set_install_default()
        self.assertIn("fresh.default-release", (self.moz / "installs.ini.bak-passkey-qr").read_text())

    def test_other_installs_keep_their_profile(self):
        other = "\n[0123456789ABCDEF]\nDefault=other.default\nLocked=1\n"  # an ESR / tarball install
        self.write("profiles.ini", DEB_INI)
        self.write("installs.ini", INSTALLS_INI + other)
        self.note_migration()
        firefox.set_install_default()
        installs = (self.moz / "installs.ini").read_text()
        self.assertIn("[0123456789ABCDEF]\nDefault=other.default", installs)
        self.assertEqual(firefox.install_default_name(), "snap.default")

    def test_an_unparsable_profiles_ini_is_left_alone(self):
        broken = "[Profile0]\nPath=a\nPath=b\n"  # duplicate option
        self.write("profiles.ini", broken)
        self.note_migration()
        with self.assertRaises(core.StepError):
            firefox.set_install_default()
        self.assertEqual((self.moz / "profiles.ini").read_text(), broken)
        self.assertIsNone(firefox.install_default_name())  # checks stay tolerant


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
        self.assertTrue(firefox.mozilla_repo_ok())
        firefox.install_mozilla_repo()
        self.assertEqual(self.rec.commands, [])

    def test_a_list_file_under_another_name_and_key_satisfies_the_repo(self):
        self.write(self.d / "firefox-extra.list", "deb-src [signed-by=/k/other.gpg] https://packages.mozilla.org/apt/ mozilla main\n")
        self.pin()
        self.assertTrue(firefox.mozilla_repo_ok())
        firefox.install_mozilla_repo()
        self.assertEqual(self.rec.commands, [])

    def test_a_deb822_file_satisfies_the_repo(self):
        self.write(self.d / "mozilla.sources", self.SOURCES)
        self.pin()
        self.assertTrue(firefox.mozilla_repo_ok())
        firefox.install_mozilla_repo()
        self.assertEqual(self.rec.commands, [])

    def test_deb822_multi_value_uris_and_suites_count(self):
        self.write(self.d / "m.sources", "Types: deb\nURIs: https://example.org/x https://packages.mozilla.org/apt/\n"
                   "Suites: stable mozilla\nComponents: main\n")
        self.assertEqual([p.name for p, _ in firefox.mozilla_definitions()], ["m.sources"])

    def test_things_that_are_not_a_definition_do_not_count(self):
        self.write(self.root / "sources.list", "# " + self.LIST_LINE)
        self.write(self.d / "off.sources", self.SOURCES.replace("Types: deb\n", "Types: deb\nEnabled: no\n"))
        self.write(self.d / "host.list", "deb https://packages.example.org/apt mozilla main\n")
        self.write(self.d / "suite.list", "deb https://packages.mozilla.org/apt other main\n")
        self.write(self.d / "notes.txt", self.LIST_LINE)
        self.write(self.d / "stanza.sources", "Types: deb\nURIs: https://example.org\nSuites: mozilla\n\n"
                   "Types: deb\nURIs: https://packages.mozilla.org/apt\nSuites: other\n")
        self.assertEqual(firefox.mozilla_definitions(), [])
        self.pin()
        self.assertFalse(firefox.mozilla_repo_ok())

    def test_a_pin_in_any_preferences_file_counts_and_a_commented_one_does_not(self):
        self.write(self.d / "mozilla.sources", self.SOURCES)
        self.write(self.prefs / "x", "# Pin: origin packages.mozilla.org\n")
        self.assertFalse(firefox.mozilla_repo_ok())
        self.pin()
        self.assertTrue(firefox.mozilla_repo_ok())

    def test_the_repo_is_not_ok_while_apt_does_not_know_it(self):
        self.write(self.d / "mozilla.sources", self.SOURCES)
        self.pin()
        self.patched(out=lambda cmd, timeout=20: "")
        self.assertFalse(firefox.mozilla_repo_ok())

    def test_nothing_defined_writes_key_list_and_pin_then_updates(self):
        firefox.install_mozilla_repo()
        self.assertEqual(self.written(), [firefox.MOZ_LIST, firefox.MOZ_PIN])
        self.assertTrue(any(c[:2] == ["sudo", "curl"] for c in self.rec.commands))
        self.assertTrue(self.updated())

    def test_a_definition_without_a_pin_writes_only_the_pin(self):
        self.write(self.d / "mozilla.sources", self.SOURCES)
        firefox.install_mozilla_repo()
        self.assertEqual(self.written(), [firefox.MOZ_PIN])
        self.assertFalse(any(c[:2] == ["sudo", "curl"] for c in self.rec.commands))

    def conflict(self):
        self.write(self.d / "mozilla.sources", self.SOURCES)
        self.write(self.d / "mozilla.list", self.LIST_LINE)
        self.pin()

    def test_two_definitions_with_different_keys_are_reported_with_the_exact_fix_and_nothing_is_changed(self):
        self.conflict()
        self.assertFalse(firefox.mozilla_repo_ok())
        with self.assertRaises(core.StepError) as raised:
            firefox.install_mozilla_repo()
        message = str(raised.exception)
        self.assertIn(str(self.d / "mozilla.sources"), message)
        self.assertIn(str(self.d / "mozilla.list"), message)
        self.assertIn(f"sudo rm {self.d / 'mozilla.list'}", message)
        self.assertEqual(self.rec.commands, [])
        self.assertTrue((self.d / "mozilla.list").exists() and (self.d / "mozilla.sources").exists())

    def test_status_shows_the_conflict_under_the_row(self):
        self.conflict()
        action = firefox.firefox_actions()[0]
        core.show_actions([action])
        self.assertIn(f"sudo rm {self.d / 'mozilla.list'}", self.rec.text)

    def test_check_names_the_conflict(self):
        self.conflict()
        self.patched(status_rows=lambda: [(firefox.firefox_actions()[0].name, False, True)])
        with self.assertRaises(SystemExit):
            cli.cmd_check(None)
        self.assertIn(f"sudo rm {self.d / 'mozilla.list'}", self.rec.text)

    def test_two_definitions_with_the_same_key_are_not_a_conflict(self):
        self.write(self.d / "a.list", self.LIST_LINE)
        self.write(self.d / "b.list", self.LIST_LINE)
        self.assertIsNone(firefox.mozilla_conflict())

    def test_a_definition_without_signed_by_next_to_one_with_it_is_a_conflict(self):
        self.write(self.d / "a.list", "deb https://packages.mozilla.org/apt mozilla main\n")
        self.write(self.d / "b.list", self.LIST_LINE)
        self.assertIn("sudo rm", firefox.mozilla_conflict() or "")

    def test_three_definitions_name_every_file_that_has_to_go(self):
        self.write(self.d / "mozilla.sources", self.SOURCES)
        self.write(self.d / "other.sources", self.SOURCES.replace("mozilla.asc", "other.asc"))
        self.write(self.d / "mozilla.list", self.LIST_LINE)
        message = firefox.mozilla_conflict() or ""
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
        with self.assertRaises(core.StepError) as raised:
            firefox.install_mozilla_repo()
        self.assertIn("Conflicting values set for option Signed-By", str(raised.exception))
        self.assertIn("sudo rm", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
