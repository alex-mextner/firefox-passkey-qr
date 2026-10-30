"""passkey-qr, step 3: Bluetooth detection, the ha-ble-vhci bridge and the Home Assistant proxy status."""

import dataclasses
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import BLUETOOTH, HOME, SENTINEL, Base, bluetooth, cli, core


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
        cli.step_bluetooth()
        self.assertEqual(self.rec.commands, [])
        self.assertIn("nothing to do", self.rec.text)

    def all_pending(self):
        """Every Bluetooth action reports 'not applied' whatever this machine looks like."""
        original = bluetooth.bluetooth_actions
        self.patched(bluetooth_actions=lambda: [dataclasses.replace(a, check=lambda: False) for a in original()])

    def test_no_bluetooth_but_home_assistant_installs_the_bridge(self):
        self.set_flags(dry_run=True, yes=True)
        self.all_pending()
        self.patched(ha_candidates=lambda: self.HA, ha_token=lambda: SENTINEL, adapter_appears=lambda: None)
        cli.step_bluetooth()
        joined = [" ".join(c) for c in self.rec.commands]
        self.assertTrue(any("pip install" in c and bluetooth.BRIDGE_REPO in c for c in joined), joined)
        self.assertIn("systemctl --user enable ha-ble-vhci", joined)
        self.assertIn("sudo modprobe hci_vhci", joined)
        self.assertIn((bluetooth.BRIDGE_CFG / "token", 0o600), self.rec.files)
        self.assertIn((bluetooth.BRIDGE_CFG / "env", 0o600), self.rec.files)

    def test_the_token_is_never_printed_or_put_on_a_command_line(self):
        self.set_flags(dry_run=True, yes=True)
        self.all_pending()
        self.patched(ha_candidates=lambda: self.HA, ha_token=lambda: SENTINEL, adapter_appears=lambda: None)
        cli.step_bluetooth()
        everything = self.rec.text + "\n".join(" ".join(c) for c in self.rec.commands)
        self.assertNotIn(SENTINEL, everything)

    def stateful(self):
        """Bluetooth actions that are 'not applied' until their apply ran (no machine state involved)."""
        original = bluetooth.bluetooth_actions

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
            self.assertFalse(cli.step_bluetooth())
        self.assertEqual(len(core.NOTES), 1)
        self.assertIn("USB Bluetooth", core.NOTES[0])
        self.assertEqual([p for p, _ in self.rec.files], [])

    def test_yes_without_a_token_waits_for_one_and_never_prompts(self):
        self.set_flags(yes=True)
        self.stateful()
        self.patched(ha_candidates=lambda: self.HA, ha_token=lambda: None, adapter_appears=lambda: None)
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")), mock.patch(
            "getpass.getpass", side_effect=AssertionError("prompted")
        ):
            self.assertFalse(cli.step_bluetooth())
        self.assertIn("needs a long-lived admin token", core.NOTES[0])
        self.assertEqual([p for p, _ in self.rec.files], [])
        # the actions that come before the token (installing the bridge) did run
        self.assertTrue(self.bridge_commands())

    def test_dry_run_with_yes_lists_the_bridge_without_a_token(self):
        self.set_flags(dry_run=True, yes=True)
        self.all_pending()
        self.patched(ha_candidates=lambda: self.HA, ha_token=lambda: None, adapter_appears=lambda: None)
        cli.step_bluetooth()
        self.assertEqual(core.NOTES, [])
        self.assertIn("systemctl --user enable ha-ble-vhci", [" ".join(c) for c in self.rec.commands])

    def test_yes_does_not_retry_a_rejected_stored_token(self):
        self.set_flags(yes=True)
        self.patched(ha_url_configured=lambda: self.HA[0], read_text=lambda path: "old-token\n")
        with mock.patch.dict(os.environ, {}, clear=False) as env:
            env.pop("HA_TOKEN", None)
            with self.assertRaises(core.NeedsYou) as raised:
                bluetooth.configure_ha()
        self.assertIn("needs a long-lived admin token", str(raised.exception))
        self.assertEqual(self.rec.files, [])

    def test_yes_replaces_a_rejected_token_with_a_fresh_one_from_the_environment(self):
        self.set_flags(yes=True)
        self.patched(ha_url_configured=lambda: self.HA[0], read_text=lambda path: "old-token\n")
        with mock.patch.dict(os.environ, {"HA_TOKEN": SENTINEL}):
            bluetooth.configure_ha()
        self.assertIn((bluetooth.BRIDGE_CFG / "token", 0o600), self.rec.files)
        self.assertNotIn(SENTINEL, self.rec.text)


class Detection(Base):
    def test_adapters_come_from_the_configured_directory(self):
        self.assertEqual(bluetooth.bt_adapters(), [])
        (Path(BLUETOOTH) / "hci0").mkdir()
        (Path(BLUETOOTH) / "rfkill0").mkdir()
        self.assertEqual(bluetooth.bt_adapters(), ["hci0"])
        self.assertIsNone(bluetooth.virtual_adapter())

    def test_virtual_adapter_is_recognised_by_its_sysfs_path(self):
        real = Path(tempfile.mkdtemp()) / "devices/virtual/bluetooth/hci1"
        real.mkdir(parents=True)
        (Path(BLUETOOTH) / "hci1").symlink_to(real)
        self.assertEqual(bluetooth.virtual_adapter(), "hci1")


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
        self.assertGreater(bluetooth.version_tuple("0.1.10"), bluetooth.version_tuple("0.1.9"))
        self.assertEqual(bluetooth.version_tuple("0.2"), bluetooth.version_tuple("0.2.0"))
        self.assertEqual(bluetooth.version_tuple("0.1.6+local"), bluetooth.version_tuple("0.1.6"))

    def test_a_pre_release_is_older_than_its_release(self):
        for pre in ("0.1.6rc1", "0.1.6.dev0", "0.1.6a1", "0.1.6b2"):
            self.assertLess(bluetooth.version_tuple(pre), bluetooth.version_tuple("0.1.6"), pre)
            self.assertGreater(bluetooth.version_tuple(pre), bluetooth.version_tuple("0.1.5"), pre)
        self.assertLess(bluetooth.version_tuple("0.2.0rc1"), bluetooth.version_tuple("0.2"))

    def test_a_post_release_is_newer_than_its_release_and_never_downgraded(self):
        self.assertGreater(bluetooth.version_tuple("0.1.6.post1"), bluetooth.version_tuple("0.1.6"))
        self.assertGreater(bluetooth.version_tuple("0.1.6post2"), bluetooth.version_tuple("0.1.6"))
        with mock.patch.object(bluetooth, "BRIDGE_VERSION", "0.1.6"):
            self.venv("0.1.6.post1")
            self.assertTrue(bluetooth.bridge_current())

    def test_a_pre_release_of_the_pinned_version_is_upgraded(self):
        with mock.patch.object(bluetooth, "BRIDGE_VERSION", "0.1.6"):
            self.venv("0.1.6rc1")
            self.assertFalse(bluetooth.bridge_current())
            self.venv("0.1.6")
            self.assertTrue(bluetooth.bridge_current())
            self.venv("0.1.6.0")
            self.assertTrue(bluetooth.bridge_current())

    def test_a_newer_bridge_is_current_and_an_older_or_missing_one_is_not(self):
        pinned = bluetooth.BRIDGE_VERSION
        newer = ".".join([*pinned.split(".")[:-1], str(int(pinned.split(".")[-1]) + 1)])
        self.venv(newer)
        self.assertTrue(bluetooth.bridge_current())
        self.venv(pinned)
        self.assertTrue(bluetooth.bridge_current())
        self.venv("0.0.1")
        self.assertFalse(bluetooth.bridge_current())
        self.venv(None)
        self.assertFalse(bluetooth.bridge_current())

    def test_a_newer_bridge_is_never_reinstalled_or_restarted(self):
        self.venv("99.0.0")
        self.set_flags(yes=True)
        action = next(a for a in bluetooth.bluetooth_actions() if "or newer installed" in a.name)
        core.run_actions("3. Bluetooth", [action])
        self.assertEqual(self.rec.commands, [])  # no pip, no systemctl restart

    def test_an_older_bridge_is_upgraded_to_the_pinned_version(self):
        self.venv("0.0.1")
        self.set_flags(yes=True)
        action = next(a for a in bluetooth.bluetooth_actions() if "or newer installed" in a.name)
        action = dataclasses.replace(action, apply=lambda: core.run(["pip", "install", "ha-ble-vhci"]))
        with self.assertRaises(core.StepError):  # the recorded run does not change the fake venv: re-check fails
            core.run_actions("3. Bluetooth", [action])
        self.assertIn(["pip", "install", "ha-ble-vhci"], self.rec.commands)


class ProxyStatus(Base):
    """`status` shows the live proxy line (bridge --scanners, subprocess mocked); `check` ignores it."""

    def setUp(self):
        super().setUp()
        self.patched(BRIDGE_BIN=Path(HOME) / "bridge-bin", BRIDGE_CFG=Path(HOME) / "bridge-cfg")
        bluetooth.BRIDGE_CFG.mkdir(exist_ok=True)
        bluetooth.BRIDGE_BIN.write_text("")
        (bluetooth.BRIDGE_CFG / "token").write_text("secret")
        (bluetooth.BRIDGE_CFG / "env").write_text("HA_URL=http://ha:8123\n")
        self.addCleanup(bluetooth.BRIDGE_BIN.unlink)
        self.addCleanup(shutil.rmtree, bluetooth.BRIDGE_CFG, True)

    def scanners(self, returncode, stdout=""):
        return mock.patch.object(
            subprocess, "run", return_value=subprocess.CompletedProcess([], returncode, stdout, ""))

    def test_online_proxy_is_listed(self):
        out = "btproxy-kitchen (30:C9:22:D1:A5:72)  source=30:C9:22:D1:A5:72\n"
        with self.scanners(0, out) as run:
            bluetooth.show_proxy_status()
        self.assertIn("[x] a Bluetooth proxy is online in Home Assistant (btproxy-kitchen (30:C9:22:D1:A5:72))",
                      self.rec.text)
        argv = run.call_args.args[0]
        self.assertEqual(argv[1:], ["--scanners", "--url", "http://ha:8123", "--token-file", str(bluetooth.BRIDGE_CFG / "token")])
        self.assertIn("timeout", run.call_args.kwargs)

    def test_no_proxy_prints_an_actionable_hint(self):
        with self.scanners(4):
            bluetooth.show_proxy_status()
        self.assertIn("[ ] a Bluetooth proxy is online", self.rec.text)
        self.assertIn("power on / reconnect the ESP32", self.rec.text)
        self.assertIn("journalctl --user -u ha-ble-vhci", self.rec.text)

    def test_unanswerable_query_prints_nothing(self):
        with self.scanners(1):
            bluetooth.show_proxy_status()
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 30)):
            bluetooth.show_proxy_status()
        self.assertEqual(self.rec.said, [])

    def test_status_shows_it_only_when_the_bridge_provides_bluetooth(self):
        self.patched(status_groups=lambda: [("3. Bluetooth via Home Assistant", [])], missing_apt=lambda: [])
        with self.scanners(0, "p  source=1\n"):
            self.patched(real_adapters=lambda: [])
            cli.cmd_status(None)
            self.assertIn("a Bluetooth proxy is online", self.rec.text)
            self.rec.said.clear()
            self.patched(real_adapters=lambda: ["hci0"])
            cli.cmd_status(None)
        self.assertNotIn("proxy", self.rec.text)

    def test_check_does_not_ask(self):
        def guarded(argv, *args, **kwargs):
            self.assertNotIn("--scanners", argv)
            return subprocess.CompletedProcess(argv, 1, "", "")

        with mock.patch.object(subprocess, "run", side_effect=guarded):
            cli.status_rows()


if __name__ == "__main__":
    unittest.main()
