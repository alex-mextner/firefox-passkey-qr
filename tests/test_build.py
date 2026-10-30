"""passkey-qr, build dependencies: the PATH the builds run under and resolving a failed build's missing packages."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import apt, build, core, credentialsd


class Dependencies(unittest.TestCase):
    def test_a_too_old_tool_is_recognised_as_a_source_built_tool(self):
        log = "Program blueprint-compiler found: NO found 0.19.0 but need: '>= 0.20.4' (/usr/bin/blueprint-compiler)"
        self.assertEqual(apt.find_missing(log), [("tool", "blueprint-compiler")])
        self.assertIn("blueprint-compiler", build.SOURCE_BUILDERS)

    def test_hyphenated_tool_names_are_captured_whole(self):
        self.assertEqual(apt.find_missing("glib-compile-resources is not in PATH"), [("bin", "glib-compile-resources")])
        self.assertEqual(apt.find_missing("blueprint-compiler is not in PATH"), [("bin", "blueprint-compiler")])

    def test_missing_system_pieces_map_to_apt_packages_without_apt_file(self):
        self.assertEqual(apt.find_missing('ERROR: Dependency "udev" not found, tried pkgconfig'), [("pc", "udev")])
        self.assertEqual(apt.find_missing("x.h:1:10: fatal error: 'stdbool.h' file not found"), [("hdr", "stdbool.h")])
        self.assertEqual(apt.find_missing("xmllint is not in PATH"), [("bin", "xmllint")])
        with mock.patch.object(core, "say", lambda *a, **k: None):
            self.assertEqual(apt.resolve_packages([("pc", "udev"), ("hdr", "stdbool.h"), ("bin", "xmllint")]),
                             ["systemd-dev", "libclang-dev", "libxml2-utils"])

    def test_cargo_and_rustc_come_from_rustup_never_from_apt(self):
        with mock.patch.object(core, "say", lambda *a, **k: None), mock.patch.object(
                apt, "apt_file_search", side_effect=AssertionError("searched apt for cargo")):
            self.assertEqual(apt.resolve_packages([("bin", "cargo"), ("bin", "rustc")]), [])

    def test_the_rust_check_follows_the_exit_status_for_any_channel_spelling(self):
        for channel in ("1.97.1", "stable", "nightly-2026-01-01", "1.97.1-x86_64-unknown-linux-gnu"):
            with mock.patch.object(credentialsd, "rust_channel", lambda c=channel: c), mock.patch.object(
                    subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"cargo 1.97.1\n", b"")) as run:
                self.assertTrue(credentialsd.rust_ok(), channel)
                self.assertEqual(run.call_args.args[0][2:4], [channel, "cargo"])
        with mock.patch.object(credentialsd, "rust_channel", lambda: "stable"), mock.patch.object(
                subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"toolchain not installed")):
            self.assertFalse(credentialsd.rust_ok())

    def test_local_tool_directories_then_system_directories_come_first_exactly_once(self):
        brew = "/home/linuxbrew/.linuxbrew/bin"
        with mock.patch.dict(os.environ, {"PATH": f"{build.LOCAL_BIN}:{brew}:/usr/bin:/bin:/opt/x"}):
            build.ensure_local_bin_on_path()
            build.ensure_local_bin_on_path()
            self.assertEqual(os.environ["PATH"], ":".join(
                [str(core.CARGO_BIN), str(build.LOCAL_BIN), *build.SYSTEM_PATH, brew, "/opt/x"]))

    def test_a_user_path_with_brew_first_still_resolves_python3_to_the_system_one(self):
        with mock.patch.dict(os.environ, {"PATH": "/home/linuxbrew/.linuxbrew/bin:/usr/bin"}):
            build.ensure_local_bin_on_path()
            self.assertLess(os.environ["PATH"].index("/usr/bin"), os.environ["PATH"].index("/home/linuxbrew"))

    def test_blueprint_is_configured_from_scratch_so_a_stale_build_dir_cannot_keep_a_wrong_python(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(core, "SRC_DIR", Path(tmp)), mock.patch.object(
                core, "run"), mock.patch.object(build, "run_resolving") as resolving:
            (Path(tmp) / "blueprint-compiler" / ".git").mkdir(parents=True)
            build.build_blueprint()
        setup = resolving.call_args_list[0].args[0]
        self.assertEqual(setup[:3], ["meson", "setup", "_build"])
        self.assertIn("--wipe", setup)
        self.assertNotIn("--reconfigure", setup)


if __name__ == "__main__":
    unittest.main()
