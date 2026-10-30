"""passkey-qr, the request watcher: only a pending passkey request wakes the checks."""

import json
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from support import SelfHealingBase, health, selfheal


class RequestOnlyChecks(SelfHealingBase):
    """Nothing runs while nobody uses a passkey: no timer, no Home Assistant query, no notification."""

    def test_the_watcher_unit_is_a_service_without_any_timer(self):
        files = selfheal.watch_unit_files()
        self.assertEqual([path.name for path in files], ["passkey-qr-watch.service"])
        text = next(iter(files.values()))
        self.assertIn(f'ExecStart="{selfheal.HEALTH_BIN}" watch\n', text)
        self.assertIn("Restart=always\n", text)
        self.assertIn("StartLimitIntervalSec=0\n", text)
        for forbidden in ("[Timer]", "OnUnitActiveSec", "OnCalendar", "OnBootSec"):
            self.assertNotIn(forbidden, text)

    def test_health_without_a_pending_request_does_nothing_at_all(self):
        called = []
        self.patched(pending_helpers=lambda: [], health=lambda now=None: called.append("health") or 0,
                     scanners_result=lambda: called.append("query"))
        health.cmd_health(None)
        self.assertEqual((called, self.notices, self.rec.commands), ([], [], []))
        self.assertIn("no passkey request is pending", self.rec.text)

    def test_health_with_a_pending_request_does_one_pass_and_settles_it(self):
        passes = []
        self.patched(pending_helpers=lambda: [(1, 5)], health=lambda now=None: passes.append(1) or 0)
        health.save_health_state({"pending": True, "issues": {"k": {"open": True, "fails": 2}}})
        with self.assertRaises(SystemExit) as ended:
            health.cmd_health(None)
        self.assertEqual((passes, ended.exception.code), ([1], 0))
        self.assertFalse(health.load_health_state()["pending"])

    def test_a_pass_without_a_pending_request_never_asks_home_assistant(self):
        self.loaded("ha-ble-vhci")
        asked = []
        self.patched(real_adapters=lambda: [], bt_adapters=lambda: ["hci0"], unit_age=lambda unit: 500,
                     scanners_result=lambda: asked.append(1))
        self.answers["systemctl --user is-active ha-ble-vhci"] = "active"
        self.answers["bluetoothctl show"] = "Powered: yes"
        health.health_bluetooth({}, 1000)
        self.assertEqual(asked, [])
        health.health_bluetooth({"pending": True}, 1000)
        self.assertEqual(asked, [1])

    def test_a_failure_without_a_pending_request_only_reaches_the_journal(self):
        health.report({}, "k", False, 1000, up=("up", "b"), down=("down", "b"))
        self.assertEqual(self.notices, [])
        self.assertIn("k: FAILING (1x): down", self.rec.text)


class ServeRequest(SelfHealingBase):
    def setUp(self):
        super().setUp()
        self.helpers, self.passes, self.sleeps = [], [], []
        self.patched(pending_helpers=lambda: self.helpers, health=lambda now=None: self.passes.append(1) or 0)

    def test_a_request_without_a_helper_is_checked_exactly_once(self):
        health.serve_request(sleep=self.sleeps.append)
        self.assertEqual((len(self.passes), self.sleeps), (1, []))

    def test_a_waiting_helper_keeps_the_request_pending_and_is_rechecked_every_interval(self):
        self.helpers = [(10, 5)]

        def sleep(seconds):
            self.sleeps.append(seconds)
            if len(self.sleeps) == 3:
                self.helpers = []  # the request ended during the third wait

        health.serve_request(sleep=sleep)
        self.assertEqual(self.sleeps, [health.REQUEST_CHECK_SECONDS] * 3)
        self.assertEqual(len(self.passes), 3)  # now, after wait 1, after wait 2: not after the one it ended in

    def test_a_request_that_ended_while_waiting_is_not_checked_or_announced_again(self):
        self.helpers = [(10, 5)]
        self.sleeps_seen = []

        def sleep(seconds):
            self.sleeps_seen.append(seconds)
            self.helpers = []

        health.serve_request(sleep=sleep)
        self.assertEqual((len(self.passes), self.notices), (1, []))

    def test_the_marker_exists_exactly_while_the_request_is_served(self):
        seen = []
        self.patched(health=lambda now=None: seen.append(health.REQUEST_MARKER.exists()) or 0)
        self.assertFalse(health.REQUEST_MARKER.exists())
        health.serve_request(sleep=self.sleeps.append)
        self.assertEqual(seen, [True])
        self.assertFalse(health.REQUEST_MARKER.exists())

    def test_a_crashing_pass_still_clears_the_marker_and_the_pending_flag(self):
        def boom(now=None):
            raise RuntimeError("boom")

        self.patched(health=boom)
        health.save_health_state({"pending": True})
        with self.assertRaises(RuntimeError):
            health.serve_request(sleep=self.sleeps.append)
        self.assertFalse(health.REQUEST_MARKER.exists())
        self.assertFalse(health.load_health_state()["pending"])

    def test_the_end_of_a_request_settles_what_was_told_but_keeps_the_rate_limit(self):
        health.save_health_state({"pending": True, "ha_down": {"since": 1, "last": 2},
                              "issues": {"k": {"open": True, "fails": 3, "told": 5}}})
        health.serve_request(sleep=self.sleeps.append)
        state = health.load_health_state()
        self.assertEqual(state["issues"]["k"], {"open": False, "fails": 0, "told": 5})
        self.assertNotIn("ha_down", state)
        self.assertFalse(state["pending"])

    def test_status_shows_whether_a_request_is_pending(self):
        health.show_request_status()
        self.assertIn("none pending", self.rec.text)
        health.mark_request(True)
        health.show_request_status()
        self.assertIn("pending since", self.rec.said[-1])

    def test_a_dry_run_writes_no_marker(self):
        self.set_flags(dry_run=True)
        health.mark_request(True)
        self.assertFalse(health.REQUEST_MARKER.exists())

    def test_passes_do_not_overlap(self):
        with health.pass_lock(wait=True) as first, health.pass_lock(wait=False) as second:
            self.assertEqual((first, second), (True, False))
        with health.pass_lock(wait=False) as again:
            self.assertTrue(again)  # released with the first holder

    def test_an_unusable_runtime_directory_does_not_stop_the_pass(self):
        self.patched(PASS_LOCK=Path("/proc/nonexistent/pass.lock"))
        with health.pass_lock(wait=True) as held:
            self.assertTrue(held)

    def test_a_manual_pass_leaves_a_request_the_watcher_is_serving_alone(self):
        self.helpers = [(1, 5)]
        health.mark_request(True)
        health.cmd_health(None)
        self.assertEqual(self.passes, [])
        self.assertIn("serving this request already", self.rec.text)

    def test_a_manual_pass_does_not_start_while_another_pass_runs(self):
        self.helpers = [(1, 5)]
        with health.pass_lock(wait=True):
            health.cmd_health(None)
        self.assertEqual(self.passes, [])
        self.assertIn("another health pass is running", self.rec.text)


class Watcher(SelfHealingBase):
    PORTAL = "org.freedesktop.portal.Desktop"

    def message(self, member="CreateCredential", at=1000.0, **over):
        data = {"type": "method_call", "destination": self.PORTAL, "interface": health.REQUEST_INTERFACE,
                "member": member, "timestamp-realtime": int(at * 1e6)}
        data.update(over)
        return json.dumps(data) + "\n"

    def register(self, app="org.mozilla.firefox", at=1000.0):
        return self.message("Register", at, interface=health.REGISTRY_INTERFACE,
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
        with mock.patch.object(subprocess, "Popen", side_effect=lambda *a, **k: order.append("monitor") or monitor) as popen, \
                mock.patch.object(time, "time", side_effect=lambda: next(clock)), \
                mock.patch.object(time, "sleep") as sleep, \
                self.assertRaises(SystemExit) as ended:
            health.cmd_watch(None)
        self.order = order
        return served, popen, sleep, ended.exception

    def test_only_calls_to_the_portal_that_start_a_passkey_are_requests(self):
        self.assertEqual(health.request_call_time(self.message()), 1000.0)
        self.assertEqual(health.request_call_time(self.message("GetCredential", 5.5)), 5.5)
        self.assertEqual(health.request_call_time(self.register()), 1000.0)  # the helper registers first
        for other in (self.message("GetClientCapabilities"),
                      self.message(interface="org.freedesktop.DBus.Properties"),
                      self.message(destination="org.example.Other"),  # same interface, some other service
                      self.message(**{"type": "signal"}), self.register("org.gnome.Other"),
                      self.message("Register", interface=health.REGISTRY_INTERFACE),  # no payload
                      "not json\n", "[1]\n", "Monitoring bus message stream.\n"):
            self.assertIsNone(health.request_call_time(other), other)

    def test_a_message_without_a_timestamp_counts_as_now(self):
        line = json.dumps({"type": "method_call", "destination": self.PORTAL, "interface": health.REQUEST_INTERFACE,
                           "member": "GetCredential"})
        with mock.patch.object(time, "time", return_value=42.0):
            self.assertEqual(health.request_call_time(line), 42.0)

    def test_the_monitor_only_listens_for_the_two_portal_calls_that_start_a_request(self):
        _, popen, _, _ = self.run_watch([])
        argv = popen.call_args.args[0]
        self.assertEqual(argv[:4], ["busctl", "--user", "monitor", "--json=short"])
        rules = [argv[i + 1] for i, word in enumerate(argv) if word == "--match"]
        self.assertEqual(rules, list(health.REQUEST_MATCHES))
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
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [health.REQUEST_SETTLE_SECONDS] * 2)

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
        health.mark_request(True)
        self.run_watch([])
        self.assertFalse(health.REQUEST_MARKER.exists())

    def test_a_dead_monitor_ends_the_watcher_so_systemd_restarts_it(self):
        _, _, _, ended = self.run_watch([])
        self.assertIn("busctl monitor ended (exit 1)", str(ended.code))


if __name__ == "__main__":
    unittest.main()
