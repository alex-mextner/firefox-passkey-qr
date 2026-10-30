"""passkey-qr, the command line: install, check, status, --help and how a failed step is reported."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

from support import BLUETOOTH, HOME, ROOT, SCRIPT, Base, clear_bluetooth, cli, core


class FailureKeepsNotes(unittest.TestCase):
    def test_a_later_failure_still_lists_what_already_waits_for_you(self):
        script = Path(HOME) / "fail.py"
        script.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from passkey_qr import cli, core\n"
            "core.NOTES.append('3. Bluetooth: needs a token')\n"
            "def boom(ns): raise core.StepError('credentialsd build failed')\n"
            "sys.argv = ['passkey-qr', '--dry-run', 'status']\n"
            "cli.cmd_status = boom\n"
            "cli.main()\n")
        result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60,
                                env={**os.environ, "HOME": HOME})
        self.assertEqual(result.returncode, 1)
        self.assertIn("credentialsd build failed", result.stderr)
        self.assertIn("still waiting for you: 3. Bluetooth: needs a token", result.stderr)


class Install(Base):
    def run_install(self):
        self.patched(STEPS=[], log_snapshot=lambda: None, open_test_page=lambda: None)
        with self.assertRaises(SystemExit) as raised:
            cli.cmd_install(None)
        return raised.exception.code

    def test_something_waiting_for_you_exits_3_and_is_listed(self):
        self.patched(status_rows=lambda: [("Firefox (Mozilla deb)", True, True)])
        core.NOTES.append("3. Bluetooth: put a token in $HA_TOKEN")
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
        cli.cmd_install(None)  # returns, no SystemExit: a context row does not make the install unfinished
        self.assertIn("Restart Firefox once", self.rec.text)
        self.assertIn("[x] Firefox (Mozilla deb)", self.rec.text)

    def test_dry_run_never_fails_on_state(self):
        self.set_flags(dry_run=True, yes=True)
        self.patched(STEPS=[])
        core.NOTES.append("3. Bluetooth: put a token in $HA_TOKEN")
        cli.cmd_install(None)
        self.assertIn("Would need you", self.rec.text)


class Check(Base):
    def rows(self, *missing):
        names = ["Firefox (Mozilla deb)", "Bluetooth adapter (local or ha-ble-vhci)", "credentialsd installed"]
        return [(name, name not in missing, True) for name in names] + [("ha-ble-vhci service", False, False)]

    def run_check(self, rows):
        self.patched(status_rows=lambda: rows)
        with self.assertRaises(SystemExit) as raised:
            cli.cmd_check(None)
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


class HealthCommandLine(unittest.TestCase):
    def test_help_lists_the_health_commands(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=60,
                                env={**os.environ, "HOME": HOME})
        for command in ("watch", "health", "heal-prefs"):
            self.assertIn(command, result.stdout)
        self.assertNotIn("45 s", result.stdout)


if __name__ == "__main__":
    unittest.main()
