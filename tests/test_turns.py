"""Fast turns: the effort each turn gets, what a reply is told, the page's checks. No CLI or model calls."""
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from solvewright import runtime as rt

INSTRUCTIONS = Path(rt.__file__).resolve().parent / "interviewer"
REPLY = {"message": "Part 2. Orders can now be amended.", "title": "Bid budget", "caseFamily": "reservations",
         "concepts": ["invariants"], "stage": "Amendments", "part": 2, "parts": 2, "finished": False}


def at(minutes):
    return (datetime(2026, 11, 10, 13, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


class TurnTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.runtime = rt.Runtime(root / "interviews", INSTRUCTIONS, root / "logs")
        self.scope = "owner-a"
        self.state = {"id": str(uuid.uuid4()), "at": at(0), "settings": {**rt.DEFAULT_SETTINGS, "provider": "claude", "effort": "xhigh"},
                      "busy": False, "finished": False, "messages": [{"role": "assistant", "text": "Build a bid budget.", "at": at(1)}],
                      "testRuns": [], "error": "", "title": "Bid budget", "caseFamily": "", "concepts": [], "stage": "Reservations", "activity": ""}
        self.directory = self.runtime.directory(self.scope, self.state["id"])
        (self.directory / "workspace" / "tests").mkdir(parents=True)
        (self.directory / ".examiner").mkdir()
        (self.directory / "workspace" / "solution.py").write_text("def reserve(q, p):\n    return q * p\n")
        self.runtime.save(self.scope, self.state)

    # Effort -----------------------------------------------------------------
    def effort(self, kind, provider="claude", **settings):
        return self.runtime.turn_effort({**self.state["settings"], **settings}, kind, provider)

    def test_replies_run_at_medium_while_design_and_marks_keep_the_chosen_effort(self):
        self.assertEqual(self.effort("prepare"), "xhigh")
        self.assertEqual(self.effort("mark"), "xhigh")
        self.assertEqual(self.effort("reply"), "medium")
        self.assertEqual(self.effort("reply", effort="low"), "low")
        self.assertEqual(self.effort("reply", effort=""), "medium")

    def test_top_firm_and_reported_rounds_keep_high_effort_replies(self):
        self.assertEqual(self.effort("reply", difficulty="stretch"), "xhigh")
        self.assertEqual(self.effort("reply", case="portals", effort=""), "high")
        self.assertEqual(self.effort("reply", difficulty="stretch", effort="max"), "xhigh")

    def test_a_nearly_spent_plan_window_lowers_reply_effort_one_step(self):
        self.runtime.claude_limits = {"status": "allowed_warning", "rateLimitType": "five_hour", "utilization": 0.81}
        self.assertEqual(self.effort("reply"), "low")
        self.assertEqual(self.effort("reply", difficulty="stretch"), "high")
        self.assertEqual(self.effort("prepare"), "xhigh")
        self.assertEqual(self.runtime.rate_limits("claude")["window"], "five_hour")

    def test_codex_limits_and_model_efforts_are_respected(self):
        class Codex:
            closed = type("E", (), {"is_set": staticmethod(lambda: False)})()
            def rate_limits(self):
                return {"primary": {"usedPercent": 80, "resetsAt": 1, "windowDurationMins": 300}, "secondary": {"usedPercent": 10}, "planType": "plus"}
        self.runtime.codex = Codex()
        self.runtime._codex_models = (0, [{"id": "gpt-6.1-sol", "default": True, "efforts": ["minimal", "high", "xhigh"]}])
        self.assertEqual(self.runtime.rate_limits("codex")["fiveHour"]["usedPercent"], 80)
        # Window nearly spent: medium becomes low, and this model offers nothing between minimal and high.
        self.assertEqual(self.effort("reply", provider="codex", model="gpt-6.1-sol"), "minimal")

    # What a reply is told ---------------------------------------------------
    def test_a_reply_is_told_the_changes_results_and_active_time_not_to_reread(self):
        self.runtime.remember_turn(self.directory)
        (self.directory / "workspace" / "solution.py").write_text("def reserve(q, p):\n    return q * p if q > 0 else 0\n")
        self.state["messages"] += [{"role": "user", "text": "hint", "at": at(5)}, {"role": "assistant", "text": "Think of zero.", "at": at(7)},
                                   {"role": "user", "text": "Review my current code for this part.", "at": at(11)}]
        self.state["testRuns"] = [{"at": at(10), "exitCode": 1, "timedOut": False, "output": "", "runner": "browser",
                                   "tests": [{"name": "test_one", "outcome": "passed"}, {"name": "test_zero", "outcome": "failed"}],
                                   "hidden": [{"name": "test_hidden.py::test_cancel", "label": "cancel releases the cash", "outcome": "failed"}]}]
        with patch.object(rt, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.fromisoformat(at(12).replace("Z", "+00:00"))
            prompt = self.runtime.continuation(self.state, self.directory, "Review my current code for this part.")
        self.assertIn("Candidate active time so far: 8 of 50", prompt)  # 11 minutes from the opening, less the 2-minute hint and the open 1 minute
        self.assertIn("+    return q * p if q > 0 else 0", prompt)
        self.assertIn("Public tests, run in the browser at 13:10 UTC: 1 of 2 passed; failing: test_zero", prompt)
        self.assertIn("Your private checks, same run: 0 of 1 passed; failing: cancel releases the cash", prompt)
        self.assertIn("Do not re-read the instructions", prompt)
        self.assertIn("Do not ask the candidate to explain code that already works", prompt)

    def test_an_old_round_without_a_record_of_the_last_reply_reads_the_files(self):
        prompt = self.runtime.continuation(self.state, self.directory, "hint")
        self.assertIn("Read the current files in workspace/", prompt)
        self.runtime.remember_turn(self.directory)
        self.assertIn("have not changed since your last reply", self.runtime.continuation(self.state, self.directory, "hint"))

    def test_a_reply_turn_shows_its_steps_and_runs_at_reply_effort(self):
        seen = {}
        def turn(state, directory, prompt, checkpoint, cancelled=None, effort=None, watch=None):
            seen["effort"] = effort
            watch({"type": "tool_use", "name": "Bash", "input": {"command": "python3 -m pytest -q"}})
            seen["during"] = json.loads((self.directory / "state.json").read_text())
            return REPLY
        self.state["messages"].append({"role": "user", "text": "Review my current code for this part.", "at": at(3)})
        self.runtime.save(self.scope, self.state)
        with patch.object(self.runtime, "claude_turn", side_effect=turn):
            self.runtime._turn(self.scope, self.state["id"], "Review my current code for this part.", False)
        self.assertEqual(seen["effort"], "medium")
        self.assertEqual(seen["during"]["turn"]["kind"], "reply")
        self.assertEqual([step["id"] for step in seen["during"]["turn"]["steps"]], ["checks"])
        self.assertEqual(seen["during"]["activity"], "Running its checks")
        after = self.runtime.load(self.scope, self.state["id"])
        self.assertNotIn("turn", after)
        self.assertEqual(after["part"], 2)
        self.assertTrue((self.directory / ".examiner" / "after-turn.json").is_file())

    def test_ending_the_round_is_a_marking_turn_at_the_chosen_effort(self):
        seen = {}
        with patch.object(self.runtime, "claude_turn", side_effect=lambda *args, **kwargs: seen.update(kwargs) or {**REPLY, "finished": True}):
            self.runtime._turn(self.scope, self.state["id"], "end", False)
        self.assertEqual(seen["effort"], "xhigh")

    # Ending without marks --------------------------------------------------
    def test_a_quiet_round_ends_without_marks_at_once_and_stays_ended(self):
        ended = self.runtime.abandon(self.scope, self.state["id"])
        self.assertTrue(ended["finished"] and ended["abandoned"])
        self.assertNotIn("reportError", ended)
        with self.assertRaisesRegex(ValueError, "ended without marks"):
            self.runtime.start_turn(self.scope, self.state["id"], "hint")
        exported = self.runtime.export_sync(self.scope)["sessions"][0]["state"]
        self.assertTrue(exported["abandoned"])

    def test_a_round_being_prepared_closes_without_marks_when_its_turn_stops(self):
        seen = {}
        def turn(state, directory, prompt, checkpoint, cancelled=None, effort=None, watch=None):
            seen["reply"] = self.runtime.abandon(self.scope, self.state["id"])
            self.assertTrue(cancelled.is_set())
            raise ValueError("The interviewer turn was stopped. Send a message to continue.")
        self.state.update(messages=[], busy=True)
        self.runtime.save(self.scope, self.state)
        self.runtime.cancellations[self.state["id"]] = rt.threading.Event()
        with patch.object(self.runtime, "claude_turn", side_effect=turn):
            self.runtime._turn(self.scope, self.state["id"], "Start a new live runtime interview.", True)
        self.assertTrue(seen["reply"]["ending"])
        after = self.runtime.load(self.scope, self.state["id"])
        self.assertEqual((after["finished"], after["abandoned"], after["busy"], after["error"]), (True, True, False, ""))
        self.assertNotIn("turn", after)

    # Ending now ---------------------------------------------------------------
    def busy_turn(self, message, reply_during):
        """Runs a turn whose agent calls end() mid-turn; returns what end() answered and the turn's cancel flag."""
        seen = {}
        def turn(state, directory, prompt, checkpoint, cancelled=None, effort=None, watch=None):
            seen["reply"] = self.runtime.end(self.scope, self.state["id"])
            seen["cancelled"] = cancelled.is_set()
            return reply_during(cancelled)
        self.state.update(busy=True)
        self.runtime.save(self.scope, self.state)
        self.runtime.cancellations[self.state["id"]] = rt.threading.Event()
        with patch.object(self.runtime, "claude_turn", side_effect=turn), patch.object(rt.threading, "Thread") as thread:
            self.runtime._turn(self.scope, self.state["id"], message, False)
        return seen, thread

    def test_ending_a_quiet_round_starts_its_marks_at_once_and_takes_no_more_messages(self):
        with patch.object(rt.threading, "Thread") as thread:
            ended = self.runtime.end(self.scope, self.state["id"])
        self.assertEqual((ended["ending"], ended["busy"], ended["activity"]), (True, True, "Writing your marks"))
        self.assertEqual(ended["messages"][-1]["role"], "user")
        self.assertEqual(ended["messages"][-1]["text"], "end")
        self.assertEqual(thread.call_args.kwargs["args"][2], "end")
        stalled = self.runtime.load(self.scope, self.state["id"])
        stalled["busy"] = False  # the marking turn failed: the round stays ended, and can be marked again
        self.runtime.save(self.scope, stalled)
        with self.assertRaisesRegex(ValueError, "being marked"):
            self.runtime.start_turn(self.scope, self.state["id"], "hint")
        with patch.object(rt.threading, "Thread"):
            self.assertTrue(self.runtime.end(self.scope, self.state["id"])["busy"])

    def test_ending_during_a_reply_stops_it_then_writes_the_marks(self):
        def stopped(cancelled):
            raise ValueError("The interviewer turn was stopped. Send a message to continue.")
        seen, thread = self.busy_turn("Review my code", stopped)
        self.assertTrue(seen["reply"]["ending"])
        self.assertTrue(seen["cancelled"])
        after = self.runtime.load(self.scope, self.state["id"])
        self.assertEqual((after["ending"], after["busy"], after["error"], after["activity"]), (True, True, "", "Writing your marks"))
        self.assertEqual(after["messages"][-1]["text"], "end")
        self.assertEqual(thread.call_args.kwargs["args"][2], "end")
        self.assertIn("ending", self.runtime.public(self.scope, after))

    def test_ending_during_the_marking_turn_lets_it_finish(self):
        seen, thread = self.busy_turn("end", lambda cancelled: {**REPLY, "finished": True})
        self.assertFalse(seen["cancelled"])
        after = self.runtime.load(self.scope, self.state["id"])
        self.assertTrue(after["finished"])
        self.assertNotIn("ending", after)
        thread.assert_not_called()

    def test_a_marking_turn_is_told_the_round_has_ended(self):
        seen = {}
        with patch.object(self.runtime, "claude_turn", side_effect=lambda state, directory, prompt, *args, **kwargs: seen.update(prompt=prompt) or {**REPLY, "finished": True}):
            self.runtime._turn(self.scope, self.state["id"], "end", False)
        self.assertIn(rt.END_RULE, seen["prompt"])

    def test_ending_a_round_still_being_prepared_ends_it_without_marks(self):
        self.state.update(messages=[])
        self.runtime.save(self.scope, self.state)
        ended = self.runtime.end(self.scope, self.state["id"])
        self.assertEqual((ended["finished"], ended["abandoned"]), (True, True))

    # Deleting -------------------------------------------------------------------
    def test_deleting_a_round_removes_everything_but_never_a_round_at_work(self):
        self.state["busy"] = True
        self.runtime.save(self.scope, self.state)
        with self.assertRaisesRegex(ValueError, "still working"):
            self.runtime.delete(self.scope, self.state["id"])
        self.state["busy"] = False
        self.runtime.save(self.scope, self.state)
        self.assertEqual(self.runtime.delete(self.scope, self.state["id"]), {"ok": True, "id": self.state["id"]})
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.runtime.sessions(self.scope), [])
        with self.assertRaisesRegex(ValueError, "not found"):
            self.runtime.delete(self.scope, self.state["id"])

    # The page's checks -------------------------------------------------------
    def test_the_page_gets_the_private_checks_and_their_labels_only(self):
        checks = self.directory / ".examiner" / "checks"
        checks.mkdir()
        (checks / "test_hidden.py").write_text("from solution import reserve\n\ndef test_cancel():\n    assert reserve(1, 2) == 2\n")
        (checks / "notes.md").write_text("private")
        (checks / "checks.json").write_text(json.dumps({"checks": [{"id": "test_hidden.py::test_cancel", "part": 1,
                                                                     "label": "cancel releases the cash", "note": "Follow the cash.", "answer": "x"}]}))
        (self.directory / ".examiner" / "reference.py").write_text("SECRET")
        result = self.runtime.checks(self.scope, self.state["id"])
        self.assertEqual([file["path"] for file in result["files"]], ["test_hidden.py"])
        self.assertEqual(result["checks"], [{"id": "test_hidden.py::test_cancel", "part": 1, "label": "cancel releases the cash", "note": "Follow the cash."}])
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(rt.Runtime.checks(self.runtime, self.scope, self.state["id"])["files"][0]["path"], "test_hidden.py")

    def test_a_round_without_checks_gets_none(self):
        self.assertEqual(self.runtime.checks(self.scope, self.state["id"]), {"files": [], "checks": []})

    def test_a_browser_run_keeps_the_private_checks_beside_the_public_tests(self):
        run = rt.clean_test_run({"exitCode": 1, "timedOut": False, "output": "", "tests": [{"name": "test_one", "outcome": "passed"}],
                                 "hidden": [{"name": "test_hidden.py::test_cancel", "label": "cancel releases the cash", "outcome": "failed", "message": "assert 2 == 3"}]})
        self.assertEqual(run["hidden"][0]["label"], "cancel releases the cash")
        self.assertNotIn("label", run["tests"][0])
        self.assertNotIn("hidden", rt.clean_test_run({"exitCode": 0, "timedOut": False, "tests": []}))


if __name__ == "__main__":
    unittest.main()
