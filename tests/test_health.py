"""passkey-qr, the health pass: repairs, stuck requests and the scanner journal."""

import os
import signal
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from support import SelfHealingBase, health


class StuckRequests(SelfHealingBase):
    PS = ("  100  900 /usr/bin/python3 /usr/local/bin/credentialsd-firefox-helper\n"
          "  101   30 /usr/bin/python3 /usr/local/bin/credentialsd-firefox-helper\n"
          "  102 5000 /usr/lib/firefox/firefox -contentproc\n"
          "  103 5000 vim notes-about-credentialsd-firefox-helper.txt\n")

    def setUp(self):
        super().setUp()
        self.loaded(*health.CREDENTIALSD_UNITS)
        self.answers["ps -u"] = self.PS
        self.real_still_helper = health.still_helper
        self.patched(still_helper=lambda pid: True)

    def test_only_helpers_older_than_the_limit_are_found(self):
        self.assertEqual(health.stuck_helpers(600), [(100, 900)])
        self.assertEqual(health.stuck_helpers(10), [(100, 900), (101, 30)])

    def test_a_stuck_helper_is_ended_by_pid_and_credentialsd_and_its_ui_are_restarted(self):
        self.answers["ps -u"] = "  100  900 /usr/bin/python3 /usr/local/bin/credentialsd-firefox-helper\n"
        with mock.patch.object(os, "kill") as sigkill:
            health.health_stuck_requests({}, 0)
        sigkill.assert_called_once_with(100, signal.SIGKILL)
        self.assertEqual(self.commands_starting("systemctl", "--user", "restart"),
                         [["systemctl", "--user", "restart", unit] for unit in health.CREDENTIALSD_UNITS])

    def test_a_request_in_flight_is_not_aborted_by_the_restart_which_waits_until_it_ends(self):
        state = {}
        with mock.patch.object(os, "kill") as sigkill:
            health.health_stuck_requests(state, 0)  # PS: helper 100 is stuck, helper 101 (30 s old) is a live request
        sigkill.assert_called_once_with(100, signal.SIGKILL)
        self.assertEqual(self.commands_starting("systemctl", "--user", "restart"), [])
        self.answers["ps -u"] = "  101   60 /usr/bin/python3 /usr/local/bin/credentialsd-firefox-helper\n"
        health.health_stuck_requests(state, 45)
        self.assertEqual(self.commands_starting("systemctl", "--user", "restart"), [])  # still busy
        self.answers["ps -u"] = ""
        health.health_stuck_requests(state, 90)  # the request ended: now it is safe
        self.assertEqual(len(self.commands_starting("systemctl", "--user", "restart")), 2)
        health.health_stuck_requests(state, 135)
        self.assertEqual(len(self.commands_starting("systemctl", "--user", "restart")), 2)  # once, not forever

    def test_a_restart_that_waited_for_a_younger_request_is_done_when_the_last_request_ends(self):
        with mock.patch.object(os, "kill"):
            health.save_health_state({"restart_credentialsd": True})
            self.answers["ps -u"] = "  101   60 /usr/bin/python3 /usr/local/bin/credentialsd-firefox-helper\n"
            health.end_request()  # a request is still alive: not now
            self.assertEqual(self.commands_starting("systemctl", "--user", "restart"), [])
            self.assertTrue(health.load_health_state()["restart_credentialsd"])
            self.answers["ps -u"] = ""
            health.end_request()  # the last one is gone
        self.assertEqual(len(self.commands_starting("systemctl", "--user", "restart")), 2)
        self.assertFalse(health.load_health_state()["restart_credentialsd"])

    def test_a_request_that_ends_without_a_stuck_one_restarts_nothing(self):
        self.answers["ps -u"] = ""
        health.end_request()
        self.assertEqual(self.commands_starting("systemctl", "--user", "restart"), [])

    def test_a_recycled_pid_is_never_ended(self):
        self.patched(still_helper=lambda pid: False)  # the helper exited and another process got its pid
        with mock.patch.object(os, "kill") as sigkill:
            health.health_stuck_requests({}, 0)
        sigkill.assert_not_called()

    def test_still_helper_reads_the_live_command_line(self):
        self.patched(still_helper=self.real_still_helper)
        self.assertFalse(health.still_helper(2 ** 22 + 1))  # no such process
        self.assertFalse(health.still_helper(os.getpid()))  # a live process that is not the helper
        cmdline = b"python3\x00/usr/local/bin/credentialsd-firefox-helper\x00"
        with mock.patch.object(Path, "read_bytes", return_value=cmdline):
            self.assertTrue(health.still_helper(1234))

    def test_a_helper_started_through_env_is_found(self):
        self.answers["ps -u"] = "  300 5000 /usr/bin/env python3 /usr/local/bin/credentialsd-firefox-helper\n"
        self.assertEqual(health.stuck_helpers(600), [(300, 5000)])

    def test_tools_that_merely_mention_the_helper_are_not_the_helper(self):
        self.answers["ps -u"] = ("  200 5000 grep credentialsd-firefox-helper\n"
                                 "  201 5000 gdb /usr/local/bin/credentialsd-firefox-helper\n"
                                 "  202 5000 /usr/local/bin/credentialsd-firefox-helper\n")
        self.assertEqual(health.stuck_helpers(600), [(202, 5000)])

    def test_a_young_request_is_left_alone(self):
        self.answers["ps -u"] = "  101   30 /usr/bin/python3 /usr/local/bin/credentialsd-firefox-helper\n"
        with mock.patch.object(os, "kill") as sigkill:
            health.health_stuck_requests({}, 0)
        sigkill.assert_not_called()
        self.assertEqual(self.rec.commands, [])

    def test_the_threshold_can_be_lowered_for_a_demo(self):
        with mock.patch.dict(os.environ, {"PASSKEY_QR_STUCK_SECONDS": "10"}), \
                mock.patch.object(os, "kill") as sigkill:
            health.health_stuck_requests({}, 0)
        self.assertEqual([c.args[0] for c in sigkill.call_args_list], [100, 101])

    def test_a_helper_that_vanished_meanwhile_is_not_an_error(self):
        with mock.patch.object(os, "kill", side_effect=ProcessLookupError):
            health.health_stuck_requests({}, 0)

    def test_dry_run_ends_nothing(self):
        self.set_flags(dry_run=True)
        with mock.patch.object(os, "kill") as sigkill:
            health.health_stuck_requests({}, 0)
        sigkill.assert_not_called()


class HealthRepairs(SelfHealingBase):
    def test_a_portal_without_the_credential_interface_is_restarted(self):
        ready = iter([False, True])
        self.patched(portal_credential_ready=lambda: next(ready))
        health.health_portal({}, 1000)
        self.assertEqual(self.commands_starting("systemctl", "--user", "restart"),
                         [["systemctl", "--user", "restart", "xdg-desktop-portal"]])
        self.assertEqual(self.notices, [])

    def test_a_portal_that_stays_broken_is_restarted_once_per_cooldown_and_told_once(self):
        self.patched(portal_credential_ready=lambda: False)
        state = {"pending": True}
        with mock.patch.object(time, "sleep"), \
                mock.patch.object(time, "monotonic", side_effect=iter(range(0, 1000, 21))):
            health.health_portal(state, 1000)
            health.health_portal(state, 1045)
            health.health_portal(state, 1090)
        self.assertEqual(len(self.commands_starting("systemctl", "--user", "restart", "xdg-desktop-portal")), 1)
        self.assertEqual(self.notices, [("The credential portal is not working", True)])

    def test_a_portal_that_never_gets_the_interface_is_left_alone_after_a_few_restarts(self):
        self.patched(portal_credential_ready=lambda: False)
        state = {}
        with mock.patch.object(time, "sleep"), \
                mock.patch.object(time, "monotonic", side_effect=iter(range(0, 100000, 21))):
            for tick in range(10):
                health.health_portal(state, 1000 + tick * (health.RESTART_COOLDOWN + 1))
        self.assertEqual(len(self.commands_starting("systemctl", "--user", "restart", "xdg-desktop-portal")),
                         health.MAX_PORTAL_RESTARTS)

    def test_a_clock_set_back_does_not_block_repairs(self):
        state = {"acted": {"restart-portal": 5000}}
        self.assertTrue(health.may_act(state, "restart-portal", 1000, health.RESTART_COOLDOWN))

    def test_a_healthy_portal_is_left_alone(self):
        self.patched(portal_credential_ready=lambda: True)
        health.health_portal({}, 0)
        self.assertEqual(self.rec.commands, [])

    def test_credentialsd_and_its_ui_are_started_on_demand_and_restarted_when_that_fails(self):
        self.loaded(*health.CREDENTIALSD_UNITS)
        answers = iter(["", "u 1", "u 2"])  # Credentials fails once then starts; UiControl is up
        self.answers["busctl --user call org.freedesktop.DBus"] = lambda cmd: next(answers)
        health.health_credentialsd({}, 1000)
        self.assertEqual(self.commands_starting("systemctl", "--user", "restart"),
                         [["systemctl", "--user", "restart", "xyz.iinuwa.credentialsd.Credentials"]])

    def test_a_stopped_bridge_service_is_started(self):
        self.loaded("ha-ble-vhci")
        self.patched(real_adapters=lambda: [], bt_adapters=lambda: [], scanners_result=lambda: None)
        self.answers["systemctl --user is-active ha-ble-vhci"] = "inactive"
        health.health_bluetooth({}, 1000)
        self.assertIn(["systemctl", "--user", "restart", "ha-ble-vhci"], self.rec.commands)

    def test_an_unpowered_adapter_is_powered_on(self):
        self.patched(real_adapters=lambda: ["hci0"], bt_adapters=lambda: ["hci0"])
        shown = iter(["Powered: no", "Powered: yes"])
        self.out_calls = []
        self.answers["bluetoothctl power on"] = lambda cmd: self.out_calls.append(cmd) or ""
        self.answers["bluetoothctl show"] = lambda cmd: next(shown)
        health.health_bluetooth({}, 0)
        self.assertIn(["bluetoothctl", "power", "on"], self.out_calls)  # bounded by a timeout, not via run()
        self.assertEqual(self.notices, [])

    def test_a_bridge_without_an_adapter_gets_one_restart_per_cooldown(self):
        self.loaded("ha-ble-vhci")
        self.patched(real_adapters=lambda: [], bt_adapters=lambda: [], scanners_result=lambda: None,
                     unit_age=lambda unit: 500)
        self.answers["systemctl --user is-active ha-ble-vhci"] = "active"
        with mock.patch.object(time, "sleep"):
            state = {}
            health.health_bluetooth(state, 1000)
            health.health_bluetooth(state, 1045)
        self.assertEqual(len(self.commands_starting("systemctl", "--user", "restart", "ha-ble-vhci")), 1)

    def test_a_bridge_the_user_disabled_is_left_alone(self):
        self.loaded("ha-ble-vhci")
        self.answers["systemctl --user is-enabled"] = "disabled"
        self.answers["systemctl --user is-active ha-ble-vhci"] = "inactive"
        self.patched(real_adapters=lambda: [], bt_adapters=lambda: [], scanners_result=lambda: None)
        health.health_bluetooth({}, 1000)
        self.assertEqual((self.rec.commands, self.notices), ([], []))

    def test_a_stopped_bridge_is_started_at_most_once_per_cooldown(self):
        self.loaded("ha-ble-vhci")
        self.answers["systemctl --user is-active ha-ble-vhci"] = "activating"  # systemd's own Restart= is pending
        self.patched(real_adapters=lambda: [], bt_adapters=lambda: [], scanners_result=lambda: None)
        state = {}
        for tick in range(4):
            health.health_bluetooth(state, 1000 + 45 * tick)
        self.assertEqual(len(self.commands_starting("systemctl", "--user", "restart", "ha-ble-vhci")), 1)

    def test_a_crashing_bridge_is_not_blamed_on_home_assistant(self):
        crashed = subprocess.CompletedProcess([], 1, "", "Traceback (most recent call last):\n  File x\nImportError: y")
        self.patched(scanners_result=lambda: crashed, scanner_log_says_none=lambda now: False)
        state = {}
        for now in (1000, 1045, 1090):
            health.health_proxy(state, now)
        self.assertEqual(self.notices, [])

    def test_a_graphical_session_without_the_systemd_target_is_still_a_session(self):
        self.answers["systemctl --user is-active graphical-session.target"] = "inactive"
        self.answers["systemctl --user show-environment"] = "LANG=C\nDISPLAY=:0\n"
        self.assertTrue(health.session_ready())
        self.answers["systemctl --user show-environment"] = "LANG=C\n"
        self.assertFalse(health.session_ready())

    def test_a_bridge_that_has_only_just_started_is_not_reported_as_missing_its_adapter(self):
        self.loaded("ha-ble-vhci")
        self.patched(real_adapters=lambda: [], bt_adapters=lambda: [], scanners_result=lambda: None,
                     unit_age=lambda unit: 20)
        self.answers["systemctl --user is-active ha-ble-vhci"] = "active"
        state = {}
        for tick in range(4):
            health.health_bluetooth(state, 1000 + 45 * tick)
        self.assertEqual((self.notices, self.commands_starting("systemctl", "--user", "restart")), ([], []))

    def test_a_bridge_that_never_registers_an_adapter_is_reported_once_settled(self):
        self.loaded("ha-ble-vhci")
        self.patched(real_adapters=lambda: [], bt_adapters=lambda: [], scanners_result=lambda: None,
                     unit_age=lambda unit: 500)
        self.answers["systemctl --user is-active ha-ble-vhci"] = "active"
        state = {"pending": True}
        with mock.patch.object(time, "sleep"):
            for now in (1000, 1045, 1090):  # the first run restarts the bridge and gives it time
                health.health_bluetooth(state, now)
        self.assertEqual(self.notices, [("No Bluetooth adapter", True)])

    def test_a_mistyped_stuck_limit_falls_back_to_the_default(self):
        with mock.patch.dict(os.environ, {"PASSKEY_QR_STUCK_SECONDS": "10min"}):
            self.assertEqual(health.stuck_limit(), health.STUCK_SECONDS)

    def test_the_state_is_saved_after_every_check_so_a_cut_off_run_keeps_it(self):
        saves = []
        self.patched(session_ready=lambda: False, health_stuck_requests=lambda s, n: s.setdefault("a", 1),
                     health_bluetooth=lambda s, n: saves.append(dict(health.load_health_state())))
        health.health(1000)
        self.assertEqual(saves, [{"a": 1, "pending": True}])  # the first check's result was on disk before the second ran

    def test_one_crashing_check_does_not_stop_the_others(self):
        ran = []

        def boom(state, now):
            raise RuntimeError("boom")

        self.patched(session_ready=lambda: False, health_bluetooth=boom,
                     health_stuck_requests=lambda state, now: ran.append("stuck"))
        self.assertEqual(health.health(1000), 1)
        self.assertEqual(ran, ["stuck"])
        self.assertIn("boom crashed", self.rec.text)

    def test_without_a_graphical_session_the_portal_checks_are_skipped(self):
        ran = []
        self.patched(session_ready=lambda: False, health_bluetooth=lambda s, n: None,
                     health_portal=lambda s, n: ran.append("portal"),
                     health_credentialsd=lambda s, n: ran.append("credentialsd"),
                     health_stuck_requests=lambda s, n: None)
        self.assertEqual(health.health(1000), 0)
        self.assertEqual(ran, [])

    def test_the_state_survives_between_runs(self):
        self.patched(session_ready=lambda: False, health_bluetooth=lambda s, n: s.setdefault("seen", n),
                     health_stuck_requests=lambda s, n: None)
        health.health(1000)
        self.assertEqual(health.load_health_state()["seen"], 1000)

    def test_the_checks_run_in_order_when_there_is_a_graphical_session(self):
        ran = []
        self.patched(session_ready=lambda: True, health_bluetooth=lambda s, n: ran.append("bluetooth"),
                     health_portal=lambda s, n: ran.append("portal"),
                     health_credentialsd=lambda s, n: ran.append("credentialsd"),
                     health_stuck_requests=lambda s, n: ran.append("stuck"))
        health.health(1000)
        self.assertEqual(ran, ["stuck", "portal", "credentialsd", "bluetooth"])  # the one that can wait on the network last

    def test_a_bridge_too_old_for_scanners_is_not_blamed_on_home_assistant(self):
        self.patched(scanners_result=lambda: subprocess.CompletedProcess([], 2, "", "unrecognized arguments"),
                     scanner_log_says_none=lambda now: False)
        state = {}
        for now in (1000, 1045, 1090):
            health.health_proxy(state, now)
        self.assertEqual(self.notices, [])

    def test_a_real_adapter_clears_an_old_proxy_outage(self):
        self.patched(real_adapters=lambda: ["hci0"], bt_adapters=lambda: ["hci0"])
        self.answers["bluetoothctl show"] = "Powered: yes"
        state = {"issues": {"bluetooth-proxy": {"fails": 5, "notified": 123}}}
        health.health_bluetooth(state, 1000)
        self.assertNotIn("bluetooth-proxy", state["issues"])

    def test_a_mistyped_fake_exit_code_falls_back_to_the_real_query(self):
        self.patched(scanners_result=lambda: subprocess.CompletedProcess([], 4, "", ""),
                     scanner_log_says_none=lambda now: False)
        with mock.patch.dict(os.environ, {"PASSKEY_QR_HEALTH_FAKE_SCANNERS_EXIT": "4x"}):
            health.health_proxy({"pending": True}, 1000)
        self.assertEqual(len(self.notices), 1)

    def test_a_healthy_system_asks_each_thing_once(self):
        self.loaded(*health.CREDENTIALSD_UNITS)
        calls = []
        self.patched(real_adapters=lambda: ["hci0"], bt_adapters=lambda: ["hci0"],
                     portal_credential_ready=lambda: calls.append("portal") or True)
        self.answers["bluetoothctl show"] = lambda cmd: calls.append("bluetoothctl") or "Powered: yes"
        self.answers["busctl --user call"] = lambda cmd: calls.append(cmd[-2]) or "u 2"
        health.health_bluetooth({}, 0)
        health.health_portal({}, 0)
        health.health_credentialsd({}, 0)
        self.assertEqual(sorted(calls), sorted(["bluetoothctl", "portal", *health.CREDENTIALSD_UNITS]))


class UnitAge(SelfHealingBase):
    def age(self, stamp, monotonic=2000.0):
        self.answers["systemctl --user show -p ActiveEnterTimestampMonotonic"] = stamp
        with mock.patch.object(time, "monotonic", return_value=monotonic):
            return health.unit_age("ha-ble-vhci")

    def test_microseconds_since_boot_become_seconds_of_age(self):
        self.assertAlmostEqual(self.age("1500000000", monotonic=2000.0), 500.0)

    def test_a_unit_that_never_started_or_an_unreadable_stamp_has_age_zero(self):
        self.assertEqual(self.age("0"), 0.0)
        self.assertEqual(self.age(""), 0.0)
        self.assertEqual(self.age("n/a"), 0.0)


class HealthDryRun(SelfHealingBase):
    def test_dry_run_does_not_persist_state(self):
        self.set_flags(dry_run=True)
        health.save_health_state({"acted": {"x": 1}})
        self.assertFalse(health.HEALTH_STATE.exists())


class ScannerJournal(SelfHealingBase):
    def line(self, age, message):
        return f"{1000 - age}.5 host ha-ble-vhci[1]: {message}"

    def says_none(self, text, unit_age=3600.0):
        self.patched(unit_age=lambda unit: unit_age)
        self.answers["journalctl --user -u ha-ble-vhci"] = text
        return health.scanner_log_says_none(1000)

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
        health.scanner_log_says_none(1000)
        self.assertEqual(calls[0][calls[0].index("--since") + 1], "@600")  # not "-6h": earlier runs say nothing

    def test_a_service_of_unknown_age_is_never_judged(self):
        self.assertFalse(self.says_none(self.line(300, "WARNING no Bluetooth scanner is connected"), unit_age=0.0))


if __name__ == "__main__":
    unittest.main()
