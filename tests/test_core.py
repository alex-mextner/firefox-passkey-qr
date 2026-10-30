"""passkey-qr, the --yes/--remove-snap contract, the run_actions loop and the HTTP probes."""

import unittest
from pathlib import Path
from unittest import mock

from support import Base, core, credentialsd, extension


class Confirm(Base):
    def test_yes_never_prompts(self):
        self.set_flags(yes=True)
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")):
            self.assertTrue(core.confirm("install?"))

    def test_yes_declines_the_destructive_step_without_remove_snap(self):
        self.set_flags(yes=True)
        self.assertFalse(core.confirm("remove the snap?", destructive=True))
        self.set_flags(yes=True, remove_snap=True)
        self.assertTrue(core.confirm("remove the snap?", destructive=True))

    def test_without_yes_the_user_is_asked(self):
        with mock.patch("builtins.input", side_effect=["", "nope", "y"]):
            self.assertFalse(core.confirm("a?"))  # empty answer = no
            self.assertTrue(core.confirm("a?", destructive=True))  # "nope" is re-asked, then "y"

    def test_dry_run_auto_yes_keeps_its_old_meaning(self):
        self.set_flags(dry_run=True)
        self.assertTrue(core.confirm("a?"))
        self.assertTrue(core.confirm("a?", destructive=True))


class Spawning(Base):
    def test_a_missing_binary_is_a_failed_step_not_a_traceback(self):
        self.patched(run_stream=core.run_stream)  # the real one: Base replaces run, not run_stream
        with self.assertRaises(core.StepError) as raised:
            core.run_stream(["/nonexistent/binary-for-test"])
        self.assertIn("cannot run /nonexistent/binary-for-test", str(raised.exception))


class Probes(Base):
    def test_check_never_starts_the_portal(self):
        calls = []

        def fake_out(cmd, timeout=20):
            calls.append(cmd)
            return ""  # the portal's name is not owned

        self.patched(out=fake_out)
        self.assertFalse(credentialsd.portal_credential_ready())
        self.assertFalse(extension.portal_registry_ok())
        self.assertTrue(all(c[:3] == ["busctl", "--user", "list"] for c in calls), calls)  # nothing introspected

    def test_a_log_that_cannot_be_written_never_fails_the_install(self):
        with mock.patch.object(core, "STATE_DIR", Path("/proc/nonexistent/state")), mock.patch.object(core, "LOG_FILE", None):
            self.assertIsNone(core.init_log("install"))


class RunActions(Base):
    def test_needs_you_skips_the_rest_of_the_step_and_is_reported(self):
        calls = []

        def blocked():
            raise core.NeedsYou("close Firefox")

        actions = [core.Action("a", lambda: False, blocked), core.Action("b", lambda: False, lambda: calls.append("b"))]
        self.set_flags(yes=True)
        self.assertFalse(core.run_actions("2. Firefox", actions))
        self.assertEqual(calls, [])
        self.assertEqual(core.NOTES, ["2. Firefox: close Firefox"])

    def test_a_context_action_that_stays_unsatisfied_is_not_an_error(self):
        action = core.Action("c", lambda: False, lambda: None, required=False, hint="later")
        self.set_flags(yes=True)
        self.assertTrue(core.run_actions("5. x", [action]))
        self.assertIn("note: c: later", self.rec.text)

    def test_a_required_action_that_stays_unsatisfied_is_an_error(self):
        self.set_flags(yes=True)
        with self.assertRaises(core.StepError):
            core.run_actions("5. x", [core.Action("d", lambda: False, lambda: None)])


class Relogin(Base):
    def test_a_check_that_needs_a_new_login_waits_for_it_under_yes_instead_of_failing(self):
        action = core.Action("portal answers", lambda: False, lambda: None, needs_relogin=True, hint="log out and in")
        self.set_flags(yes=True)
        self.assertFalse(core.run_actions("4. credentialsd", [action]))
        self.assertEqual(core.NOTES, ["4. credentialsd: log out and in"])

    def test_the_same_check_is_an_error_when_a_person_is_present(self):
        action = core.Action("portal answers", lambda: False, lambda: None, needs_relogin=True, hint="log out and in")
        with mock.patch("builtins.input", return_value="y"), self.assertRaises(core.StepError):
            core.run_actions("4. credentialsd", [action])

    def test_a_callable_hint_is_only_evaluated_when_the_check_fails(self):
        hint = mock.Mock(return_value="late")
        self.set_flags(yes=True)
        core.run_actions("5. x", [core.Action("ok after apply", iter([False, True, True]).__next__, lambda: None, hint=hint)])
        hint.assert_not_called()


if __name__ == "__main__":
    unittest.main()
