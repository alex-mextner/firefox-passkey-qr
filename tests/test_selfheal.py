"""passkey-qr, step 6: restart policy, the installed copy of the program and the watcher/prefs units."""

import os
import shutil
import subprocess
import unittest
from pathlib import Path

from support import HOME, ROOT, SCRIPT, SelfHealingBase, cli, core, selfheal


class RestartPolicy(SelfHealingBase):
    UNITS = ("ha-ble-vhci", "xdg-desktop-portal", "xyz.iinuwa.credentialsd.Credentials",
             "xyz.iinuwa.credentialsd.UiControl", "passwords-appd")

    def test_the_dropin_restarts_forever_without_a_start_limit(self):
        for unit in self.UNITS:
            text = selfheal.restart_dropin_text(unit)
            self.assertIn("Restart=always\n", text)
            self.assertIn("StartLimitIntervalSec=0\n", text)
            self.assertRegex(text, r"RestartSec=[1-9]\n")
            self.assertLess(text.index("[Unit]"), text.index("StartLimitIntervalSec"))  # a [Unit] setting
            self.assertLess(text.index("StartLimitIntervalSec"), text.index("[Service]"))

    def test_the_whole_chain_is_covered(self):
        self.assertEqual(set(selfheal.RESTART_SEC), set(self.UNITS))

    def test_units_that_do_not_exist_are_skipped_silently(self):
        self.loaded("ha-ble-vhci")
        selfheal.write_restart_policy()
        self.assertEqual([p for p, _ in self.rec.files], [selfheal.restart_dropin("ha-ble-vhci")])
        self.assertEqual(self.commands_starting("systemctl", "--user", "daemon-reload"),
                         [["systemctl", "--user", "daemon-reload"]])

    def test_nothing_installed_means_nothing_to_do(self):
        self.loaded()
        self.assertTrue(selfheal.restart_policy_ok())

    def test_an_unreachable_user_manager_is_not_mistaken_for_nothing_to_do(self):
        self.answers["systemctl --user show -p Id"] = ""  # `systemctl --user` failed: out() returns ""
        self.assertFalse(selfheal.restart_policy_ok())
        with self.assertRaises(core.StepError):
            selfheal.write_restart_policy()

    def test_dry_run_without_a_user_manager_still_lists_the_step_instead_of_failing(self):
        self.set_flags(dry_run=True)
        self.answers["systemctl --user show -p Id"] = ""
        selfheal.write_restart_policy()  # only prints: must not raise

    def test_an_aliased_unit_name_does_not_break_the_check(self):
        self.answers["systemctl --user show -p Id"] = "Id=canonical.service\nLoadState=loaded\nRestart=always"
        self.assertTrue(selfheal.restart_policy_ok())

    def test_the_dropin_sorts_after_any_other_restart_setting(self):
        self.assertTrue(selfheal.restart_dropin("passwords-appd").name > "90-pc.conf")

    def test_the_check_needs_both_the_file_and_the_loaded_policy(self):
        self.loaded("passwords-appd")
        path = selfheal.restart_dropin("passwords-appd")
        self.assertFalse(selfheal.restart_policy_ok())  # file missing
        path.parent.mkdir(parents=True)
        path.write_text(selfheal.restart_dropin_text("passwords-appd"))
        self.assertTrue(selfheal.restart_policy_ok())
        self.loaded("passwords-appd", restart="on-failure")  # written but not daemon-reloaded
        self.assertFalse(selfheal.restart_policy_ok())

    def test_all_units_are_asked_in_one_systemctl_call(self):
        calls = []
        self.patched(out=lambda cmd, timeout=20: calls.append(cmd) or "")
        selfheal.restart_policy_ok()
        self.assertEqual(len(calls), 1)
        self.assertEqual(len([a for a in calls[0] if a.endswith(".service")]), len(selfheal.RESTART_SEC))

    def test_a_deliberate_do_not_restart_exit_status_is_never_overridden(self):
        for unit in self.UNITS:
            self.assertNotRegex(selfheal.restart_dropin_text(unit), r"(?m)^RestartPreventExitStatus=")


class SelfHealingStep(SelfHealingBase):
    def test_it_is_a_required_row_of_check_and_shown_in_status(self):
        self.assertTrue(all(a.required and not a.optional for a in selfheal.selfheal_actions() if "copy is current" not in a.name))
        copy = next(a for a in selfheal.selfheal_actions() if "copy is current" in a.name)
        self.assertFalse(copy.required)  # a checkout update makes the copy stale: install refreshes, check stays quiet
        self.assertIn("6. Self-healing", [title for title, _ in cli.status_groups()])
        self.assertIn(cli.step_selfheal, cli.STEPS)

    def checkout(self, entry, modules=None):
        """A checkout the installer copies from: the entry script plus the given passkey_qr modules."""
        root = self.root / "checkout"
        (root / "passkey_qr").mkdir(parents=True)
        (root / "passkey-qr").write_text(entry)
        for name, text in (modules or {}).items():
            (root / "passkey_qr" / name).write_text(text)
        self.patched(SOURCE_ROOT=root)
        return root

    def test_apply_writes_units_enables_them_and_the_check_then_holds(self):
        self.patched(write_file=lambda path, content, mode=0o644: (path.parent.mkdir(parents=True, exist_ok=True),
                                                                   path.write_text(content), path.chmod(mode)))
        self.checkout("#!/usr/bin/env python3\n")
        self.loaded("ha-ble-vhci")
        self.old_units()
        self.answers.update({"systemctl --user is-enabled": "enabled", "systemctl --user is-active": "active"})
        self.assertFalse(any(a.check() for a in selfheal.selfheal_actions()))
        for action in selfheal.selfheal_actions():
            action.apply()
        self.assertTrue(all(a.check() for a in selfheal.selfheal_actions()))
        self.assertIn(["systemctl", "--user", "enable", "passkey-qr-watch.service"], self.rec.commands)
        self.assertIn(["systemctl", "--user", "restart", "passkey-qr-watch.service"], self.rec.commands)
        self.assertIn(["systemctl", "--user", "enable", "--now", "passkey-qr-prefs.path"], self.rec.commands)
        service = (selfheal.SYSTEMD_USER / "passkey-qr-watch.service").read_text()
        self.assertIn(f'ExecStart="{selfheal.HEALTH_BIN}" watch', service)
        self.assertFalse(list(selfheal.SYSTEMD_USER.glob("*.timer")))  # not one timer is installed
        self.assertEqual(selfheal.HEALTH_BIN.stat().st_mode & 0o777, 0o755)

    def old_units(self):
        """What an earlier version installed: the 45 s timer and its service."""
        selfheal.SYSTEMD_USER.mkdir(parents=True, exist_ok=True)
        for name in selfheal.OBSOLETE_UNITS:
            (selfheal.SYSTEMD_USER / name).write_text("[Unit]\nDescription=old\n")

    def test_units_survive_a_home_with_a_space_or_a_percent_sign(self):
        self.patched(HEALTH_BIN=Path("/home/a b/100%/passkey-qr"))
        service = selfheal.watch_unit_files()[selfheal.SYSTEMD_USER / "passkey-qr-watch.service"]
        self.assertIn('ExecStart="/home/a b/100%%/passkey-qr" watch', service)

    def test_the_path_unit_watches_both_firefox_profile_lists(self):
        text = selfheal.prefs_unit_files()[selfheal.SYSTEMD_USER / "passkey-qr-prefs.path"]
        self.assertIn(f"PathModified={core.MOZ_PROFILE / 'profiles.ini'}", text)
        self.assertIn(f"PathModified={core.MOZ_PROFILE / 'installs.ini'}", text)

    def test_a_changed_checkout_makes_the_installed_copy_stale(self):
        checkout = self.checkout("v1")
        selfheal.install_health_copy()
        self.assertTrue(selfheal.health_copy_ok())
        (checkout / "passkey-qr").write_text("v2")
        self.assertFalse(selfheal.health_copy_ok())

    def test_dry_run_changes_nothing(self):
        self.set_flags(dry_run=True)
        self.loaded("ha-ble-vhci")
        self.old_units()
        for action in selfheal.selfheal_actions():
            action.apply()
        self.assertFalse(selfheal.HEALTH_BIN.exists())
        self.assertEqual(sorted(p.name for p in selfheal.SYSTEMD_USER.iterdir()),
                         ["passkey-qr-health.service", "passkey-qr-health.timer"])  # nothing written, nothing removed

    def test_the_installer_removes_the_45_second_timer_it_used_to_install(self):
        self.old_units()
        self.assertFalse(selfheal.obsolete_units_gone())
        selfheal.remove_obsolete_units()
        self.assertTrue(selfheal.obsolete_units_gone())
        self.assertEqual(self.commands_starting("systemctl", "--user", "disable", "--now"),
                         [["systemctl", "--user", "disable", "--now", "passkey-qr-health.timer"]])

    def test_a_refreshed_copy_restarts_the_running_watcher(self):
        self.checkout("v2")
        (selfheal.SYSTEMD_USER).mkdir(parents=True)
        (selfheal.SYSTEMD_USER / "passkey-qr-watch.service").write_text("[Service]\n")
        selfheal.install_health_copy()
        self.assertIn(["systemctl", "--user", "try-restart", "passkey-qr-watch.service"], self.rec.commands)

    def test_the_first_install_does_not_try_to_restart_a_watcher_that_does_not_exist_yet(self):
        self.checkout("v1")
        selfheal.install_health_copy()
        self.assertEqual(self.commands_starting("systemctl"), [])

    def test_the_copy_holds_the_entry_script_and_every_module(self):
        self.checkout("v1", {"core.py": "core", "health.py": "health"})
        selfheal.install_health_copy()
        target = selfheal.HEALTH_BIN.parent
        self.assertEqual(sorted(str(p.relative_to(target)) for p in target.rglob("*") if p.is_file()),
                         ["passkey-qr", "passkey_qr/core.py", "passkey_qr/health.py"])
        self.assertEqual(selfheal.HEALTH_BIN.stat().st_mode & 0o777, 0o755)
        self.assertEqual((target / "passkey_qr/core.py").stat().st_mode & 0o777, 0o644)
        self.assertEqual((target / "passkey_qr/health.py").read_text(), "health")

    def test_a_changed_module_makes_the_installed_copy_stale(self):
        checkout = self.checkout("v1", {"core.py": "a"})
        selfheal.install_health_copy()
        self.assertTrue(selfheal.health_copy_ok())
        (checkout / "passkey_qr/core.py").write_text("b")
        self.assertFalse(selfheal.health_copy_ok())
        selfheal.install_health_copy()
        self.assertTrue(selfheal.health_copy_ok())

    def test_a_module_the_checkout_no_longer_has_leaves_the_copy(self):
        checkout = self.checkout("v1", {"core.py": "a", "old.py": "b"})
        selfheal.install_health_copy()
        (checkout / "passkey_qr/old.py").unlink()
        self.assertFalse(selfheal.health_copy_ok())
        selfheal.install_health_copy()
        self.assertFalse((selfheal.HEALTH_BIN.parent / "passkey_qr/old.py").exists())
        self.assertTrue(selfheal.health_copy_ok())

    def test_the_installed_copy_runs_without_the_checkout(self):
        checkout = self.checkout("v1")
        shutil.rmtree(checkout)
        shutil.copytree(ROOT / "passkey_qr", checkout / "passkey_qr", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(SCRIPT, checkout / "passkey-qr")
        selfheal.install_health_copy()
        shutil.rmtree(checkout)
        result = subprocess.run([str(selfheal.HEALTH_BIN), "--help"], capture_output=True, text=True, timeout=60,
                                env={**os.environ, "HOME": HOME})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("heal-prefs", result.stdout)


if __name__ == "__main__":
    unittest.main()
