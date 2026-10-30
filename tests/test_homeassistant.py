"""passkey-qr, Home Assistant being down or hung: how it is told and when the container is repaired."""

import calendar
import subprocess
import unittest
from unittest import mock

from support import SelfHealingBase, health


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
            self.assertIn(expected, health.ha_reason(said), said)

    def test_an_unknown_complaint_is_passed_through_trimmed_and_an_empty_one_is_named(self):
        self.assertEqual(health.ha_reason("weird " * 40), ("weird " * 40)[:120])
        self.assertEqual(health.ha_reason(""), "no answer")

    def test_the_bridges_own_prefix_is_not_repeated_in_the_message(self):
        self.assertEqual(health.ha_reason("cannot reach Home Assistant at http://x:8123: something odd"), "something odd")
        self.assertIn("dropped the connection",
                      health.ha_reason("cannot reach Home Assistant at http://127.0.0.1:18123: Server disconnected"))


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
        health.health_proxy(state, 1000)
        self.assertEqual(self.notices, [("Home Assistant is unreachable", True)])
        body = self.bodies[0]
        self.assertIn(self.URL, body)
        self.assertIn("nothing answers on its port", body)
        self.assertIn("you are told when it is back", body)
        self.assertNotIn("exit 5", body)
        self.assertNotIn("exit 1", body)

    def test_a_failed_query_is_repeated_once_before_the_user_is_told(self):
        self.answer(5, 0)
        health.health_proxy({"pending": True}, 1000)
        self.assertEqual((self.notices, len(self.queries)), ([], 2))

    def test_a_query_that_is_fine_is_asked_once(self):
        self.answer(0)
        health.health_proxy({"pending": True}, 1000)
        self.assertEqual(len(self.queries), 1)

    def test_a_missing_proxy_is_not_repeated_and_says_the_esp32_returns_after_a_restart(self):
        self.answer(4)
        health.health_proxy({"pending": True}, 1000)
        self.assertEqual((self.notices, len(self.queries)), ([("Bluetooth proxy (ESP32) is offline", True)], 1))
        self.assertIn("has just restarted", self.bodies[0])

    def test_a_refused_token_is_not_called_unreachable(self):
        self.answer(1, reason="authentication failed: {'type': 'auth_invalid'}")
        health.health_proxy({"pending": True}, 1000)
        self.assertEqual(self.notices, [("Home Assistant refuses the passkey bridge", True)])
        self.assertIn("auth_invalid", self.bodies[0])
        self.assertIn("token", self.bodies[0])

    def test_home_assistant_answering_again_ends_the_outage_tracking(self):
        state = {"pending": True, "ha_down": {"since": 1, "last": 2}}
        self.answer(4)
        health.health_proxy(state, 1000)
        self.assertNotIn("ha_down", state)

    def test_a_bridge_crash_is_never_blamed_on_home_assistant(self):
        self.answer(70)
        health.health_proxy({"pending": True}, 1000)
        self.assertEqual(self.notices, [])

    def test_a_home_assistant_that_accepts_the_connection_but_never_answers_is_unreachable(self):
        bridge, cfg = self.root / "bridge", self.root / "cfg"
        cfg.mkdir()
        bridge.write_text("#!/bin/sh\n")
        (cfg / "token").write_text("not-a-real-token\n")
        self.patched(BRIDGE_BIN=bridge, BRIDGE_CFG=cfg)
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("bridge", 15)) as run:
            health.health_proxy({"pending": True}, 1000)
        self.assertEqual(run.call_count, 1)  # it already waited its full timeout: not asked a second time
        self.assertEqual(self.notices, [("Home Assistant is unreachable", True)])
        self.assertIn("no answer in time", self.bodies[0])

    def test_an_unconfigured_bridge_is_still_not_an_outage(self):
        self.patched(BRIDGE_BIN=self.root / "missing")
        health.health_proxy({"pending": True}, 1000)
        self.assertEqual(self.notices, [])

    def test_the_outage_is_timed_across_passes_and_restarts_after_a_gap(self):
        state = {}
        self.assertEqual(health.note_ha_down(state, 1000), 0)
        self.assertEqual(health.note_ha_down(state, 1020), 20)
        self.assertEqual(health.note_ha_down(state, 1100), 100)
        self.assertEqual(health.note_ha_down(state, 1100 + health.HA_DOWN_GAP_SECONDS + 1), 0)  # a new outage


class HomeAssistantRepair(SelfHealingBase):
    """Looking at, and when it is really broken restarting, Home Assistant's container over the user's own SSH."""

    NOW = calendar.timegm((2026, 10, 23, 12, 0, 0))
    RUNNING = "running|healthy|2026-10-20T10:00:00.5Z|2026-10-20T09:59:00Z"
    UNHEALTHY = "running|unhealthy|2026-10-20T10:00:00.5Z|2026-10-20T09:59:00Z"
    STOPPED = "exited|none|2026-10-22T10:00:00Z|2026-10-23T08:00:00Z"

    def setUp(self):
        super().setUp()
        health.HA_REPAIR_CONFIG.parent.mkdir(parents=True)
        health.HA_REPAIR_CONFIG.write_text("HA_CONTAINER=homeassistant-homeassistant-1\n")
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
        return health.ha_container_plan(info, self.NOW, down_for)[0]

    def test_the_decision_table(self):
        self.assertEqual(self.plan(status="exited", finished=self.NOW - 3600), "start")
        self.assertEqual(self.plan(status="exited", finished=self.NOW - 5), "wait")  # Docker restarts it itself
        self.assertEqual(self.plan(status="dead"), "start")
        self.assertEqual(self.plan(status="created"), "start")
        self.assertEqual(self.plan(status="paused"), "unpause")
        self.assertEqual(self.plan(status="restarting"), "wait")
        self.assertEqual(self.plan(health="healthy", down_for=10 * 3600), "wait")  # a healthy container is never restarted
        self.assertEqual(self.plan(health="none", down_for=10 * 3600), "wait")
        self.assertEqual(self.plan(health="unhealthy", down_for=health.HA_HUNG_SECONDS - 1), "wait")
        self.assertEqual(self.plan(health="unhealthy", down_for=health.HA_HUNG_SECONDS), "restart")
        self.assertEqual(self.plan(health="unhealthy", down_for=3600, started=self.NOW - 100), "wait")  # still booting

    def test_a_stopped_container_is_started_and_the_user_is_told_so(self):
        self.inspect = self.STOPPED
        said = health.ha_repair({}, self.NOW, 0)
        self.assertEqual(self.actions(), [("odroidn2", "start", "homeassistant-homeassistant-1")])
        self.assertIn("was stopped: it was started", said)

    def test_a_core_restart_with_the_container_up_is_left_alone(self):
        said = health.ha_repair({}, self.NOW, 150)
        self.assertEqual(self.actions(), [])
        self.assertEqual(len(self.docker), 1)  # the inspect
        self.assertIn("itself is restarting", said)

    def test_a_deliberate_restart_every_few_minutes_is_never_fought(self):
        self.inspect = self.UNHEALTHY
        state = {}
        for restart in range(3):  # three core restarts, each 3 min of 'unreachable', 4 min apart
            base = self.NOW + restart * 600
            for second in range(0, 181, 20):
                health.ha_repair(state, base + second, health.note_ha_down(state, base + second))
        self.assertEqual(self.actions(), [])

    def test_a_container_hung_for_minutes_is_restarted_once(self):
        self.inspect = self.UNHEALTHY
        state = {}
        for second in range(0, 600, 20):
            health.ha_repair(state, self.NOW + second, health.note_ha_down(state, self.NOW + second))
        self.assertEqual(self.actions(), [("odroidn2", "restart", "homeassistant-homeassistant-1")])

    def test_it_is_looked_at_over_ssh_at_most_once_a_minute(self):
        state = {}
        health.ha_repair(state, self.NOW, 0)
        self.assertEqual(health.ha_repair(state, self.NOW + 20, 20), "")
        self.assertEqual(len(self.docker), 1)
        health.ha_repair(state, self.NOW + 61, 61)
        self.assertEqual(len(self.docker), 2)

    def test_a_repair_is_not_repeated_within_the_cooldown(self):
        self.inspect = self.STOPPED
        state = {}
        health.ha_repair(state, self.NOW, 0)
        said = health.ha_repair(state, self.NOW + 120, 120)  # still stopped two minutes later: Docker/ssh did not help
        self.assertEqual(len(self.actions()), 1)
        self.assertIn("was stopped", said)

    def test_without_ssh_access_nothing_is_done_and_the_user_is_told_why(self):
        self.inspect = ""
        said = health.ha_repair({}, self.NOW, 0)
        self.assertEqual(self.actions(), [])
        self.assertIn("over SSH", said)

    def test_a_failed_repair_is_reported_not_claimed(self):
        self.inspect, self.action_code = self.STOPPED, 1
        said = health.ha_repair({}, self.NOW, 0)
        self.assertIn("did not work", said)
        self.assertNotIn("it was started", said)

    def test_without_a_configured_container_nothing_is_touched(self):
        health.HA_REPAIR_CONFIG.unlink()
        self.assertEqual(health.ha_repair({}, self.NOW, 9999), "")
        self.assertEqual(self.docker, [])

    def test_a_dry_run_looks_but_never_acts(self):
        self.set_flags(dry_run=True)
        self.inspect = self.STOPPED
        health.ha_repair({}, self.NOW, 0)
        self.assertEqual(self.actions(), [])

    def test_the_ssh_host_defaults_to_the_home_assistant_host_and_can_be_set(self):
        self.assertEqual(health.ha_repair_target(), ("odroidn2", "homeassistant-homeassistant-1"))
        health.HA_REPAIR_CONFIG.write_text("HA_CONTAINER=ha\nHA_SSH_HOST=me@box\n")
        self.assertEqual(health.ha_repair_target(), ("me@box", "ha"))

    def test_values_that_could_smuggle_a_command_or_an_option_are_refused(self):
        for bad in ("HA_CONTAINER=x;reboot", "HA_CONTAINER=$(id)", "HA_CONTAINER=a|b", "HA_CONTAINER=-rf"):
            health.HA_REPAIR_CONFIG.write_text(bad + "\n")
            self.assertIsNone(health.ha_repair_target(), bad)
        health.HA_REPAIR_CONFIG.write_text("HA_CONTAINER=ha\nHA_SSH_HOST=-oProxyCommand=evil\n")
        self.assertIsNone(health.ha_repair_target())

    def test_a_missing_or_url_less_config_means_no_repair(self):
        self.patched(ha_url_configured=lambda: None)
        health.HA_REPAIR_CONFIG.write_text("HA_CONTAINER=ha\n")
        self.assertIsNone(health.ha_repair_target())
        health.HA_REPAIR_CONFIG.unlink()
        self.assertIsNone(health.ha_repair_target())

    def test_docker_runs_over_batchmode_ssh_with_every_word_quoted(self):
        self.patched(ssh_docker=self.real_ssh_docker)
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            health.ssh_docker("odroidn2", "inspect", "--format", "{{.State.Status}} x", "name")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:6], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "odroidn2"])
        self.assertEqual(argv[6], "docker inspect --format '{{.State.Status}} x' name")

    def test_ssh_that_cannot_run_is_none_not_a_crash(self):
        self.patched(ssh_docker=self.real_ssh_docker)
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 25)):
            self.assertIsNone(health.ssh_docker("h", "ps"))
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(health.ssh_docker("h", "ps"))

    def test_docker_timestamps(self):
        self.assertAlmostEqual(health.docker_time("2026-10-23T01:03:34.99516253Z"),
                               calendar.timegm((2026, 10, 23, 1, 3, 34)) + 0.99516253)
        self.assertEqual(health.docker_time("2026-10-23T01:03:34Z"), calendar.timegm((2026, 10, 23, 1, 3, 34)))
        self.assertEqual(health.docker_time("0001-01-01T00:00:00Z"), 0.0)
        self.assertEqual(health.docker_time(""), 0.0)

    def test_the_notification_carries_what_was_found_and_done(self):
        self.inspect = self.STOPPED
        self.patched(scanner_log_says_none=lambda now: False, scanner_query=lambda: (5, "Connect call failed"))
        health.health_proxy({"pending": True}, self.NOW)
        self.assertIn("Its container was stopped: it was started", self.bodies[0])
        self.assertEqual(self.notices, [("Home Assistant is unreachable", True)])


if __name__ == "__main__":
    unittest.main()
