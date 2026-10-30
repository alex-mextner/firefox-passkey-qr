"""passkey-qr, step 4: the xdg-desktop-portal configuration for credentialsd."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import core, credentialsd, patch_named


class PortalConfig(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for name, value in dict(PORTAL_CONF=self.root / "portals.conf", say=lambda *a, **k: None).items():
            patcher = patch_named(name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        core.DRY_RUN = False
        patcher = mock.patch.object(credentialsd, "system_portal_preferred",
                                    lambda: {"default": "gnome;gtk;", "org.freedesktop.impl.portal.Secret": "gnome-keyring;"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_desktops_own_backends_are_kept_and_credentialsd_is_added(self):
        self.assertFalse(credentialsd.portal_conf_ok())
        credentialsd.write_portal_conf()
        self.assertTrue(credentialsd.portal_conf_ok())
        text = credentialsd.PORTAL_CONF.read_text()
        self.assertIn("default=gnome;gtk;", text)
        self.assertIn("org.freedesktop.impl.portal.Secret=gnome-keyring;", text)
        self.assertIn("org.freedesktop.impl.portal.experimental.Credential=credentialsd", text)

    def test_an_existing_config_without_a_preferred_section_gets_the_desktop_defaults(self):
        credentialsd.PORTAL_CONF.write_text("# empty on purpose\n[other]\nx=1\n")
        credentialsd.write_portal_conf()
        self.assertTrue(credentialsd.portal_conf_ok())
        self.assertIn("default=gnome;gtk;", credentialsd.PORTAL_CONF.read_text())
        self.assertIn("[other]", credentialsd.PORTAL_CONF.read_text())  # the user's other sections stay

    def test_a_desktop_specific_user_file_carries_the_entry_too(self):
        specific = credentialsd.PORTAL_CONF.with_name("gnome-portals.conf")
        specific.write_text("[preferred]\ndefault=gnome\n")
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}):
            self.assertFalse(credentialsd.portal_conf_ok())
            credentialsd.write_portal_conf()
            self.assertTrue(credentialsd.portal_conf_ok())
        self.assertIn("org.freedesktop.impl.portal.experimental.Credential=credentialsd", specific.read_text())
        self.assertIn("default=gnome", specific.read_text())

    def test_an_unparsable_portal_config_is_not_overwritten(self):
        credentialsd.PORTAL_CONF.write_text("[preferred]\na=1\na=2\n")
        with self.assertRaises(core.StepError):
            credentialsd.write_portal_conf()
        self.assertEqual(credentialsd.PORTAL_CONF.read_text(), "[preferred]\na=1\na=2\n")

    def test_an_existing_user_config_is_extended_not_replaced(self):
        credentialsd.PORTAL_CONF.write_text("[preferred]\ndefault=kde\nmy.own.Interface=mine\n")
        credentialsd.write_portal_conf()
        text = credentialsd.PORTAL_CONF.read_text()
        self.assertIn("default=kde", text)
        self.assertIn("my.own.Interface=mine", text)
        self.assertNotIn("gnome", text)
        self.assertTrue(credentialsd.portal_conf_ok())


if __name__ == "__main__":
    unittest.main()
