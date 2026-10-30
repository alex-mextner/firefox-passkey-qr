"""passkey-qr, what the user is told on the desktop and in the journal, and how often."""

import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from support import Base, SelfHealingBase, health


class OfflineProxyNotification(SelfHealingBase):
    """`--scanners` exit 4 (no proxy) / 5 (Home Assistant unreachable) while a passkey request is pending ->
    ONE desktop notification per failure; never without a pending request."""

    def setUp(self):
        super().setUp()
        self.patched(scanner_log_says_none=lambda now: False, ha_repair=lambda state, now, down_for: "")

    def pass_with(self, code, now, *, pending=True, reason=""):
        state = health.load_health_state()
        state["pending"] = pending
        env = {"PASSKEY_QR_HEALTH_FAKE_SCANNERS_EXIT": str(code), "PASSKEY_QR_HEALTH_FAKE_SCANNERS_REASON": reason}
        with mock.patch.dict(os.environ, env):
            health.health_proxy(state, now)
        health.save_health_state(state)

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
        health.health_proxy(state, 1000)
        self.assertEqual((self.notices, state), ([], {"pending": True}))

    def test_the_real_exit_code_is_used_without_the_override(self):
        self.patched(scanners_result=lambda: subprocess.CompletedProcess([], 4, "", ""))
        health.health_proxy({"pending": True}, 1000)
        self.assertEqual(len(self.notices), 1)

    def test_the_real_unreachable_exit_code_and_stderr_reach_the_message(self):
        said = "cannot reach Home Assistant at http://x:8123: Cannot connect to host x:8123 [Connect call failed]"
        self.patched(scanners_result=lambda: subprocess.CompletedProcess([], 5, "", said + "\n"))
        health.health_proxy({"pending": True}, 1000)
        self.assertEqual(self.notices, [("Home Assistant is unreachable", True)])
        self.assertIn("nothing answers on its port", self.bodies[0])

    def test_a_bridge_that_says_none_while_home_assistant_sees_a_scanner_is_restarted_once(self):
        self.patched(scanners_result=lambda: subprocess.CompletedProcess([], 0, "p  source=1\n", ""),
                     scanner_log_says_none=lambda now: True)
        state = {"pending": True}
        health.health_proxy(state, 1000)
        health.health_proxy(state, 1020)  # inside the cooldown
        self.assertEqual(self.commands_starting("systemctl", "--user", "restart", "ha-ble-vhci"),
                         [["systemctl", "--user", "restart", "ha-ble-vhci"]])


class JournalPriority(unittest.TestCase):
    def test_the_priority_prefix_is_only_written_when_stdout_really_is_the_journal(self):
        stat = os.fstat(sys.stdout.fileno())
        with mock.patch.dict(os.environ, {"JOURNAL_STREAM": f"{stat.st_dev}:{stat.st_ino}"}):
            self.assertTrue(health.journal_is_stdout())
        with mock.patch.dict(os.environ, {"JOURNAL_STREAM": "1:2"}):  # inherited from a service: not our stdout
            self.assertFalse(health.journal_is_stdout())
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("JOURNAL_STREAM", None)
            self.assertFalse(health.journal_is_stdout())


class DesktopNotification(Base):
    def test_notify_send_gets_a_clear_title_and_a_critical_urgency_for_an_outage(self):
        with mock.patch.object(shutil, "which", return_value="/usr/bin/notify-send"), \
                mock.patch.object(subprocess, "run") as run:
            health.notify("Bluetooth proxy (ESP32) is offline", "Power it on.", urgent=True)
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "/usr/bin/notify-send")
        self.assertEqual(argv[argv.index("-u") + 1], "critical")
        self.assertEqual(argv[-2:], ["Bluetooth proxy (ESP32) is offline", "Power it on."])
        self.assertEqual(argv[argv.index("-h") + 1], "string:x-canonical-private-synchronous:passkey-qr")
        self.assertIn("notify: Bluetooth proxy (ESP32) is offline", self.rec.text)  # and the journal line

    def test_a_missing_notify_send_is_logged_not_raised(self):
        with mock.patch.object(shutil, "which", return_value=None):
            health.notify("x", "y")
        self.assertIn("notify-send is not installed", self.rec.text)

    def test_dry_run_sends_nothing(self):
        self.set_flags(dry_run=True)
        with mock.patch.object(subprocess, "run") as run:
            health.notify("x", "y")
        run.assert_not_called()

    def test_a_failing_notify_send_is_logged_not_mistaken_for_delivered(self):
        failed = subprocess.CompletedProcess([], 1, "", "Failed to connect to socket")
        with mock.patch.object(shutil, "which", return_value="/usr/bin/notify-send"), \
                mock.patch.object(subprocess, "run", return_value=failed):
            health.notify("x", "y")
        self.assertIn("notify-send failed (exit 1): Failed to connect to socket", self.rec.text)


class NotificationRules(SelfHealingBase):
    """report(): once per failure, rate limited across requests, 'recovered' only for a request that was told."""

    def failing(self, now, pending=True):
        state = health.load_health_state()
        state["pending"] = pending
        health.report(state, "k", False, now, up=("back", "ok"), down=("down", "bad"))
        health.save_health_state(state)

    def recover(self, now):
        state = health.load_health_state()
        state["pending"] = True
        health.report(state, "k", True, now, up=("back", "ok"))
        health.save_health_state(state)

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
        health.end_request()
        self.recover(1500)
        self.assertEqual(self.titles(), ["down"])

    def test_a_retrying_user_is_not_told_twice_within_the_rate_limit_but_hears_that_it_is_over(self):
        self.failing(1000)
        health.end_request()
        self.failing(1060)  # a second request while the same outage lasts
        self.assertEqual(self.titles(), ["down"])
        self.recover(1080)
        self.assertEqual(self.titles(), ["down", "back"])

    def test_a_request_after_the_rate_limit_is_told_again(self):
        self.failing(1000)
        health.end_request()
        self.failing(1000 + health.NOTIFY_MIN_SECONDS + 1)
        self.assertEqual(self.titles(), ["down", "down"])

    def test_a_clock_set_back_does_not_silence_the_user_forever(self):
        self.failing(5000)
        health.end_request()
        self.failing(1000)
        self.assertEqual(self.titles(), ["down", "down"])

    def test_a_failure_that_never_recovers_is_never_called_recovered(self):
        self.failing(1000)
        self.failing(1020)
        self.assertEqual(self.titles(), ["down"])

    def test_a_blip_is_never_announced_when_nothing_was_told(self):
        self.recover(1000)
        self.assertEqual(self.notices, [])


if __name__ == "__main__":
    unittest.main()
