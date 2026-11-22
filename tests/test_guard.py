import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone

from spendtab.guard import (
    Config,
    Event,
    append_event,
    compact_ledger,
    evaluate,
    latest_per_session,
    read_events,
    read_last_cost,
)
from spendtab.hook import guard_init, handle_pretooluse

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


def cost_state(cost):
    return json.dumps({"type": "cost-state", "totalCostUSD": cost}) + "\n"


def event(
    session_id,
    cost,
    day="2026-11-23",
    project="/work/app",
    decision="allow",
    dev="tester",
):
    return Event(
        ts=f"{day}T10:00:00+00:00",
        day=day,
        session_id=session_id,
        project=project,
        cost_usd=cost,
        dev=dev,
        tool="Bash",
        decision=decision,
    )


class TailScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "session.jsonl")

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_last_snapshot_wins(self):
        self.write(
            cost_state(1.0)
            + '{"type": "assistant"}\n'
            + cost_state(2.5)
            + '{"type": "assistant"}\n'
        )
        self.assertAlmostEqual(read_last_cost(self.path), 2.5)

    def test_small_tail_bytes_still_finds_snapshot(self):
        self.write('{"type": "assistant"}\n' + cost_state(3.75))
        self.assertAlmostEqual(read_last_cost(self.path, tail_bytes=60), 3.75)

    def test_missing_or_empty_file(self):
        self.assertIsNone(read_last_cost(os.path.join(self.tmp.name, "missing.jsonl")))
        self.write("")
        self.assertIsNone(read_last_cost(self.path))

    def test_no_snapshot(self):
        self.write('{"type": "assistant"}\n')
        self.assertIsNone(read_last_cost(self.path))

    def test_non_finite_cost_is_rejected(self):
        self.write('{"type": "cost-state", "totalCostUSD": "NaN"}\n')
        self.assertIsNone(read_last_cost(self.path))
        self.write('{"type": "cost-state", "totalCostUSD": -2}\n')
        self.assertIsNone(read_last_cost(self.path))


class EvaluateTests(unittest.TestCase):
    def config(self, **overrides):
        base = dict(
            session_budget_usd=5.0,
            daily_budget_usd=25.0,
            project_daily_budget_usd=15.0,
            mode="block",
        )
        base.update(overrides)
        return Config(**base)

    def test_under_budget_allows(self):
        decision = evaluate(
            self.config(), "s1", "/work/app", 2.0, [], now=NOW
        )
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.blocked)

    def test_session_budget_blocks(self):
        decision = evaluate(
            self.config(), "s1", "/work/app", 6.0, [], now=NOW
        )
        self.assertTrue(decision.blocked)
        self.assertIn("session", decision.reason)

    def test_daily_budget_blocks_across_sessions(self):
        events = [event("other", 20.0)]
        decision = evaluate(
            self.config(), "s1", "/work/app", 6.0, events, now=NOW
        )
        self.assertTrue(decision.blocked)
        self.assertIn("daily", decision.reason)

    def test_project_budget_blocks(self):
        events = [event("other", 10.0, project="/work/app")]
        decision = evaluate(
            self.config(), "s1", "/work/app", 6.0, events, now=NOW
        )
        self.assertTrue(decision.blocked)
        self.assertIn("project", decision.reason)

    def test_latest_snapshot_not_double_counted(self):
        events = [event("s1", 3.0), event("s1", 4.0)]
        self.assertEqual(latest_per_session(events)["s1"].cost_usd, 4.0)
        decision = evaluate(
            self.config(), "s1", "/work/app", 4.0, events, now=NOW
        )
        self.assertAlmostEqual(decision.day_today, 4.0)
        self.assertFalse(decision.blocked)

    def test_previous_days_ignored(self):
        events = [event("old", 100.0, day="2026-11-22")]
        decision = evaluate(
            self.config(), "s1", "/work/app", 1.0, events, now=NOW
        )
        self.assertFalse(decision.blocked)
        self.assertAlmostEqual(decision.day_today, 1.0)

    def test_cross_midnight_session_is_not_recharged(self):
        config = self.config(
            session_budget_usd=100.0, daily_budget_usd=100.0,
            project_daily_budget_usd=15.0,
        )
        events = [event("s1", 20.0, day="2026-11-22")]
        decision = evaluate(
            config, "s1", "/work/app", 20.0, events, now=NOW
        )
        self.assertFalse(decision.blocked)
        self.assertAlmostEqual(decision.day_today, 0.0)
        self.assertAlmostEqual(decision.project_today, 0.0)

    def test_cross_midnight_new_spend_is_charged(self):
        config = self.config(
            session_budget_usd=100.0, daily_budget_usd=100.0,
            project_daily_budget_usd=15.0,
        )
        events = [event("s1", 20.0, day="2026-11-22")]
        decision = evaluate(
            config, "s1", "/work/app", 23.0, events, now=NOW
        )
        self.assertAlmostEqual(decision.day_today, 3.0)
        self.assertAlmostEqual(decision.project_today, 3.0)

    def test_warn_mode_allows_but_reports(self):
        decision = evaluate(
            self.config(mode="warn"), "s1", "/work/app", 6.0, [], now=NOW
        )
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.blocked)
        self.assertIsNotNone(decision.reason)
        from spendtab.guard import render_check

        self.assertIn("WARN", render_check(decision))

    def test_project_override(self):
        config = self.config(
            session_budget_usd=60,
            daily_budget_usd=100,
            project_budgets={"/work/client": 100.0},
        )
        self.assertEqual(config.budget_for_project("/work/client"), 100.0)
        self.assertEqual(config.budget_for_project("/work/client/sub"), 100.0)
        self.assertEqual(config.budget_for_project("/work/other"), 15.0)
        decision = evaluate(config, "s1", "/work/client", 50.0, [], now=NOW)
        self.assertFalse(decision.blocked)

    def test_dev_daily_budget_blocks(self):
        config = self.config(dev="alice", dev_daily_budget_usd=10.0)
        events = [event("other", 8.0, dev="alice", project="/work/other")]
        decision = evaluate(
            config, "s1", "/work/app", 3.0, events, now=NOW
        )
        self.assertTrue(decision.blocked)
        self.assertIn("dev alice", decision.reason)
        self.assertAlmostEqual(decision.dev_today, 11.0)

    def test_dev_budget_does_not_double_count_current_session(self):
        config = self.config(dev="alice", dev_daily_budget_usd=10.0)
        events = [event("s1", 4.0, dev="alice")]
        decision = evaluate(
            config, "s1", "/work/app", 4.0, events, now=NOW
        )
        self.assertAlmostEqual(decision.dev_today, 4.0)
        self.assertFalse(decision.blocked)

    def test_no_dev_label_skips_dev_budget(self):
        config = self.config(dev=None, dev_daily_budget_usd=1.0)
        events = [event("other", 8.0)]
        decision = evaluate(
            config, "s1", "/work/app", 1.0, events, now=NOW
        )
        self.assertFalse(decision.blocked)
        self.assertIsNone(decision.dev_budget)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "state", "ledger.jsonl")

    def test_append_and_read(self):
        append_event(event("s1", 2.0), self.path)
        append_event(event("s1", 2.5), self.path)
        events = read_events(self.path)
        self.assertEqual(len(events), 2)
        self.assertAlmostEqual(events[1].cost_usd, 2.5)
        self.assertEqual(events[0].dev, "tester")

    def test_missing_ledger_reads_empty(self):
        self.assertEqual(read_events(os.path.join(self.tmp.name, "nope")), [])

    def test_non_finite_ledger_row_is_skipped(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write('{"day":"2026-11-23","session_id":"s","cost_usd":"NaN"}\n')
            handle.write('{"day":"2026-11-23","session_id":"s","cost_usd":1}\n')
        events = read_events(self.path)
        self.assertEqual(len(events), 1)
        self.assertAlmostEqual(events[0].cost_usd, 1.0)

    def test_non_string_dev_is_coerced(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(
                '{"day":"2026-11-23","session_id":"s","cost_usd":1,"dev":{"a":1}}\n'
            )
        events = read_events(self.path)
        self.assertEqual(events[0].dev, "{'a': 1}")

    def test_compact_drops_old_rows(self):
        append_event(event("old", 100.0, day="2026-01-01"), self.path)
        append_event(event("new", 2.0, day="2026-11-23"), self.path)
        kept = compact_ledger(self.path, keep_days=30, now=NOW)
        self.assertEqual(kept, 1)
        sessions = [e.session_id for e in read_events(self.path)]
        self.assertEqual(sessions, ["new"])


class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = os.path.join(self.tmp.name, "guard.json")
        self.ledger = os.path.join(self.tmp.name, "ledger.jsonl")
        self.transcript = os.path.join(self.tmp.name, "session.jsonl")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "session_budget_usd": 5,
                    "daily_budget_usd": 25,
                    "project_daily_budget_usd": 15,
                    "mode": "block",
                },
                handle,
            )
        self.payload = {
            "session_id": "s1",
            "cwd": "/work/app",
            "tool_name": "Bash",
            "transcript_path": self.transcript,
        }

    def write_cost(self, cost):
        with open(self.transcript, "w", encoding="utf-8") as handle:
            handle.write(cost_state(cost))

    def call(self, dry_run=False):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = handle_pretooluse(
                self.payload,
                config_file=self.config_path,
                ledger_file=self.ledger,
                dry_run=dry_run,
                now=NOW,
            )
        return code, out.getvalue(), err.getvalue()

    def test_allow_writes_ledger(self):
        self.write_cost(1.0)
        code, _, _ = self.call()
        self.assertEqual(code, 0)
        events = read_events(self.ledger)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].decision, "allow")

    def test_block_exits_two_and_does_not_run_tool(self):
        self.write_cost(6.0)
        code, _, err = self.call()
        self.assertEqual(code, 2)
        self.assertIn("budget exceeded", err)
        self.assertEqual(read_events(self.ledger)[0].decision, "block")

    def test_dry_run_writes_nothing(self):
        self.write_cost(6.0)
        code, out, _ = self.call(dry_run=True)
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["blocked"])
        self.assertFalse(os.path.exists(self.ledger))

    def test_missing_config_fails_open(self):
        self.write_cost(100.0)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = handle_pretooluse(
                self.payload,
                config_file=os.path.join(self.tmp.name, "missing.json"),
                ledger_file=self.ledger,
            )
        self.assertEqual(code, 0)

    def test_unknown_transcript_is_tolerated(self):
        self.payload = dict(self.payload, transcript_path="/nonexistent/x.jsonl")
        code, _, _ = self.call()
        self.assertEqual(code, 0)

    def test_webhook_url_validation(self):
        config = Config.from_dict({"webhook_url": "http://example.com/hook"})
        self.assertIsNone(config.webhook_url)
        config = Config.from_dict({"webhook_url": "https://example.com/hook"})
        self.assertEqual(config.webhook_url, "https://example.com/hook")
        config = Config.from_dict({"webhook_url": "https://user:pass@x.example/h"})
        self.assertIsNone(config.webhook_url)
        config = Config.from_dict({"webhook_url": "http://127.0.0.1:8080x/hook"})
        self.assertIsNone(config.webhook_url)

    def test_invalid_webhook_does_not_break_config_load(self):
        path = os.path.join(self.tmp.name, "badwebhook.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"webhook_url": "http://127.0.0.1:8080x/hook"}, handle)
        from spendtab.guard import load_config

        config = load_config(path)
        self.assertIsNotNone(config)
        self.assertIsNone(config.webhook_url)

    def test_guard_init_writes_config_and_snippet(self):
        target = os.path.join(self.tmp.name, "new", "guard.json")
        out = io.StringIO()
        with redirect_stdout(out):
            code = guard_init(target)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(target))
        self.assertIn("PreToolUse", out.getvalue())
        with open(target, "r", encoding="utf-8") as handle:
            sample = json.load(handle)
        self.assertEqual(sample["mode"], "warn")
        self.assertGreater(sample["session_budget_usd"], 0)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(guard_init(target), 1)


if __name__ == "__main__":
    unittest.main()
