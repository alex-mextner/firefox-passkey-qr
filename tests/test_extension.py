"""passkey-qr, step 5: the extension step and the Firefox prefs (USB token off, re-applied by heal-prefs)."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import Base, apt, core, extension, firefox, health, patch_named


class ExtensionStep(Base):
    def test_the_policy_is_written_before_anything_that_waits_for_a_new_login(self):
        names = [a.name for a in extension.extension_actions()]
        policy = next(i for i, n in enumerate(names) if n.startswith("Firefox policy"))
        relogin = [i for i, a in enumerate(extension.extension_actions()) if a.needs_relogin]
        self.assertTrue(relogin)
        self.assertLess(policy, min(relogin))

    def test_an_existing_desktop_file_with_the_app_id_is_never_shadowed(self):
        exports = Path(tempfile.mkdtemp())
        (exports / "applications").mkdir()
        (exports / "applications" / f"{core.FIREFOX_APP_ID}.desktop").write_text("[Desktop Entry]\n")
        with mock.patch.dict(os.environ, {"XDG_DATA_DIRS": str(exports)}):
            self.assertTrue(extension.app_desktop_exists())
            extension.write_app_desktop()
        self.assertEqual(self.rec.files, [])
        with mock.patch.dict(os.environ, {"XDG_DATA_DIRS": str(Path(tempfile.mkdtemp()))}), mock.patch.object(
                core, "HOME", Path(tempfile.mkdtemp())), mock.patch.object(extension, "FLATPAK_APPLICATION_DIRS", []):
            extension.write_app_desktop()
        self.assertEqual([p for p, _ in self.rec.files], [extension.APP_DESKTOP])

    def test_apt_never_waits_for_a_question_nobody_can_see(self):
        apt.apt_install(["zip"])
        command = self.rec.commands[-1]
        self.assertEqual(command[:4], ["sudo", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get"])
        self.assertIn("Dpkg::Options::=--force-confold", command)


class UsbTokenOff(unittest.TestCase):
    """user.js managed block: Firefox must never offer a USB security key (tmp profile dirs only)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.moz = self.root / "firefox"
        (self.moz / "snap.default").mkdir(parents=True)
        (self.moz / "fresh.default-release").mkdir()
        for name, value in dict(MOZ_PROFILE=self.moz, say=lambda *a, **k: None).items():
            patcher = patch_named(name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        core.DRY_RUN = False
        self.write("profiles.ini", f"[Install{firefox.DEB_INSTALL_HASH}]\nDefault=fresh.default-release\nLocked=1\n"
                                   "[Profile0]\nName=snap\nIsRelative=1\nPath=snap.default\nDefault=1\n")
        self.userjs = self.moz / "fresh.default-release" / "user.js"

    def write(self, name, text):
        (self.moz / name).write_text(text)

    def test_profiles_are_the_install_default_and_the_profiles_ini_default(self):
        self.assertEqual(extension.managed_profiles(),
                         [self.moz / "fresh.default-release", self.moz / "snap.default"])

    def test_missing_pref_is_pending_and_apply_writes_it(self):
        self.assertFalse(extension.usb_token_off_ok())
        extension.disable_usb_token()
        self.assertTrue(extension.usb_token_off_ok())
        for profile in extension.managed_profiles():
            text = (profile / "user.js").read_text()
            for name in ("webauthn_enable_usbtoken", "webauthn_enable_softtoken", "u2f"):
                self.assertRegex(text, rf'user_pref\("security\.webauth\.{name}", false\);')

    def test_one_profile_without_it_keeps_the_row_pending(self):
        extension.disable_usb_token()
        (self.moz / "snap.default" / "user.js").unlink()
        self.assertFalse(extension.usb_token_off_ok())

    def test_no_profile_means_not_ok(self):
        shutil.rmtree(self.moz / "fresh.default-release")
        shutil.rmtree(self.moz / "snap.default")
        self.assertFalse(extension.usb_token_off_ok())

    def test_apply_is_idempotent(self):
        extension.disable_usb_token()
        first = self.userjs.read_text()
        extension.disable_usb_token()
        self.assertEqual(self.userjs.read_text(), first)
        self.assertEqual(first.count(extension.USERJS_BEGIN), 1)

    def test_foreign_lines_survive_before_and_after_the_block(self):
        self.userjs.write_text('user_pref("my.pref", 1);\n')
        extension.disable_usb_token()
        self.userjs.write_text(self.userjs.read_text() + 'user_pref("later.pref", 2);')
        extension.disable_usb_token()
        text = self.userjs.read_text()
        self.assertIn('user_pref("my.pref", 1);', text)
        self.assertIn('user_pref("later.pref", 2);', text)
        self.assertEqual(text.count(extension.USERJS_BEGIN), 1)

    def test_a_stale_block_is_replaced(self):
        self.userjs.write_text(f'keep\n{extension.USERJS_BEGIN}\nuser_pref("security.webauth.webauthn_enable_usbtoken", true);\n'
                               f'{extension.USERJS_END}\nkeep2\n')
        self.assertFalse(extension.usb_token_off_ok())
        extension.disable_usb_token()
        text = self.userjs.read_text()
        self.assertNotIn("usbtoken\", true", text)
        self.assertTrue(extension.usb_token_off_ok())
        self.assertIn("keep\n", text)
        self.assertIn("keep2\n", text)

    def test_it_is_a_required_row_for_check(self):
        action = next(a for a in extension.extension_actions() if "USB security key" in a.name)
        self.assertTrue(action.required)


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
            patcher = patch_named(name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        core.DRY_RUN = False
        self.userjs = self.moz / "p.default" / "user.js"

    def test_drift_is_reapplied_flagged_and_only_the_managed_block_is_touched(self):
        self.userjs.write_text('user_pref("mine", 1);\n')
        health.cmd_heal_prefs(None)
        self.assertTrue(extension.usb_token_off_ok())
        self.assertIn('user_pref("mine", 1);', self.userjs.read_text())
        self.assertEqual(self.notices, [])  # nobody is using a passkey right now: journal only
        self.assertTrue(any("drifted" in line for line in self.said))

    def test_prefs_in_place_are_not_touched_and_nobody_is_told(self):
        extension.disable_usb_token()
        before = self.userjs.read_text()
        self.notices.clear()
        health.cmd_heal_prefs(None)
        self.assertEqual((self.userjs.read_text(), self.notices), (before, []))

    def test_no_profile_yet_is_not_an_error(self):
        shutil.rmtree(self.moz / "p.default")
        health.cmd_heal_prefs(None)
        self.assertEqual(self.notices, [])


if __name__ == "__main__":
    unittest.main()
