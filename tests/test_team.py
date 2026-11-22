import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from unittest.mock import patch

from tokentab.guard import Config, Event, append_event, read_events
from tokentab.hook import handle_pretooluse
from tokentab.team import build_team_report, incremental_events, render_team
from tokentab.cli import main

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


def event(
    session_id,
    cost,
    *,
    dev="alice",
    project="/work/app",
    day="2026-11-23",
    ts=None,
    decision="allow",
    delta=None,
):
    return Event(
        ts=ts or f"{day}T10:00:00+08:00",
        day=day,
        session_id=session_id,
        project=project,
        cost_usd=cost,
        dev=dev,
        tool="Bash",
        decision=decision,
        delta_usd=delta,
    )


class IncrementalTests(unittest.TestCase):
    def test_cumulative_snapshots_become_deltas(self):
        events = [
            event("s1", 1.0, ts="2026-11-23T10:00:00+08:00"),
            event("s1", 2.5, ts="2026-11-23T10:05:00+08:00"),
            event("s2", 4.0, ts="2026-11-23T10:06:00+08:00"),
        ]
        items = incremental_events(events)
        self.assertEqual([round(i.delta_usd, 2) for i in items], [1.0, 1.5, 4.0])
        self.assertAlmostEqual(sum(i.delta_usd for i in items), 6.5)

    def test_explicit_delta_is_used(self):
        events = [
            event("s1", 10.0, delta=2.0),
            event("s1", 10.0, delta=0.0),
        ]
        items = incremental_events(events)
        self.assertEqual(len(items), 1)
        self.assertAlmostEqual(items[0].delta_usd, 2.0)

    def test_regression_never_produces_negative_delta(self):
        events = [
            event("s1", 5.0, ts="2026-11-23T10:00:00+08:00"),
            event("s1", 3.0, ts="2026-11-23T10:05:00+08:00"),
        ]
        items = incremental_events(events)
        self.assertEqual(len(items), 1)
        self.assertAlmostEqual(items[0].delta_usd, 5.0)

    def test_mixed_timezone_offsets_order_by_absolute_time(self):
        # 09:00+08:00 is 01:00Z, before 02:00Z, despite the lexicographic order.
        events = [
            event("s1", 5.0, ts="2026-11-23T02:00:00+00:00"),
            event("s1", 3.0, ts="2026-11-23T09:00:00+08:00"),
        ]
        items = incremental_events(events)
        self.assertAlmostEqual(items[0].delta_usd, 3.0)
        self.assertAlmostEqual(items[1].delta_usd, 2.0)


class TeamReportTests(unittest.TestCase):
    def test_groups_dev_project_day_and_blocks(self):
        events = [
            event("s1", 2.0, dev="alice", project="/work/a", delta=2.0),
            event("s2", 5.0, dev="bob", project="/work/b", delta=5.0, decision="block"),
            event("s1", 6.0, dev="alice", project="/work/a", delta=4.0),
            event("s1", 6.0, dev="alice", project="/work/a", delta=0.0, decision="block"),
            event("old", 100.0, dev="alice", project="/work/a", day="2026-10-01", delta=100.0),
        ]
        report = build_team_report(events, days=7, now=NOW)
        self.assertAlmostEqual(report.total_usd, 11.0)
        self.assertEqual(report.sessions, 2)
        self.assertEqual(report.blocks, 2)
        self.assertTrue(report.has_labels)
        self.assertEqual(report.by_dev[0].name, "alice")
        self.assertAlmostEqual(report.by_dev[0].cost_usd, 6.0)
        self.assertAlmostEqual(report.by_project[1].cost_usd, 5.0)
        self.assertEqual(report.by_day[-1].name, "2026-11-23")
        self.assertEqual(report.by_day[-1].sessions, 2)
        text = render_team(report)
        self.assertIn("By developer", text)
        self.assertIn("alice", text)

    def test_unlabeled_and_empty(self):
        report = build_team_report([], now=NOW)
        self.assertEqual(report.total_usd, 0.0)
        self.assertFalse(report.has_labels)
        empty_text = render_team(report)
        self.assertIn("No ledger rows", empty_text)
        self.assertNotIn("note", empty_text)

        unlabeled = build_team_report(
            [event("s", 1.0, dev=None, delta=1.0)], days=7, now=NOW
        )
        self.assertEqual(unlabeled.by_dev[0].name, "(unlabeled)")
        self.assertIn("note", render_team(unlabeled))

    def test_json_shape(self):
        report = build_team_report(
            [event("s", 1.0, delta=1.0)], days=7, now=NOW
        )
        payload = report.to_dict()
        self.assertEqual(payload["kind"], "guard_report")
        self.assertEqual(payload["totals"]["sessions"], 1)
        json.dumps(payload, allow_nan=False)


class GuardReportCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = os.path.join(self.tmp.name, "ledger.jsonl")

    def test_report_json(self):
        append_event(event("s1", 2.0, delta=2.0), self.ledger)
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["guard", "report", "--ledger", self.ledger, "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertAlmostEqual(payload["totals"]["cost_usd"], 2.0)
        self.assertEqual(payload["by_dev"][0]["name"], "alice")

    def test_report_text(self):
        append_event(event("s1", 2.0, delta=2.0), self.ledger)
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["guard", "report", "--ledger", self.ledger])
        self.assertEqual(code, 0)
        self.assertIn("TokenTab guard ledger", out.getvalue())

    def test_report_csv(self):
        import csv

        append_event(event("s1", 2.0, delta=2.0), self.ledger)
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["guard", "report", "--ledger", self.ledger, "--csv"])
        self.assertEqual(code, 0)
        rows = list(csv.DictReader(io.StringIO(out.getvalue())))
        self.assertTrue(rows)
        dev_rows = [r for r in rows if r["dimension"] == "dev"]
        self.assertEqual(dev_rows[0]["name"], "alice")
        self.assertAlmostEqual(float(dev_rows[0]["cost_usd"]), 2.0)


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = os.path.join(self.tmp.name, "guard.json")
        self.ledger = os.path.join(self.tmp.name, "ledger.jsonl")
        self.transcript = os.path.join(self.tmp.name, "s1.jsonl")
        with open(self.transcript, "w", encoding="utf-8") as handle:
            handle.write('{"type": "cost-state", "totalCostUSD": 6.0}\n')
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "session_budget_usd": 5,
                    "webhook_url": "http://127.0.0.1:9999/hook",
                    "webhook_on": "block",
                    "dev": "tester",
                },
                handle,
            )
        self.payload = {
            "session_id": "s1",
            "cwd": "/work/app",
            "tool_name": "Bash",
            "transcript_path": self.transcript,
        }

    def call(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = handle_pretooluse(
                self.payload,
                config_file=self.config_path,
                ledger_file=self.ledger,
                now=NOW,
            )
        return code

    def test_webhook_fires_once_per_state_change(self):
        with patch("tokentab.hook.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b""
            self.assertEqual(self.call(), 2)
            self.assertEqual(self.call(), 2)
        self.assertEqual(urlopen.call_count, 1)
        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertIn("budget exceeded", body["text"])
        self.assertEqual(body["dev"], "tester")

    def test_webhook_disabled_with_never(self):
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "session_budget_usd": 5,
                    "webhook_url": "http://127.0.0.1:9999/hook",
                    "webhook_on": "never",
                },
                handle,
            )
        with patch("tokentab.hook.urllib.request.urlopen") as urlopen:
            self.assertEqual(self.call(), 2)
        urlopen.assert_not_called()

    def test_insecure_webhook_url_is_dropped(self):
        config = Config.from_dict({"webhook_url": "http://example.com/hook"})
        self.assertIsNone(config.webhook_url)
        config = Config.from_dict({"webhook_url": "https://example.com/hook"})
        self.assertEqual(config.webhook_url, "https://example.com/hook")
        config = Config.from_dict({"webhook_url": "https://user:pass@x.example/h"})
        self.assertIsNone(config.webhook_url)


class CsvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "projects")
        os.makedirs(os.path.join(self.root, "-work-app"))

    def test_csv_rows_and_fail_over(self):
        import csv

        path = os.path.join(self.root, "-work-app", "s1.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "cost-state",
                        "sessionId": "s1",
                        "totalCostUSD": 12.5,
                        "modelUsage": {
                            "claude-sonnet-5-5": {
                                "inputTokens": 100,
                                "outputTokens": 20,
                                "cacheReadInputTokens": 900,
                                "cacheCreationInputTokens": 50,
                                "costUSD": 12.5,
                            }
                        },
                    }
                )
                + "\n"
            )
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--root", self.root, "--csv", "--no-cache"])
        self.assertEqual(code, 0)
        rows = list(csv.reader(io.StringIO(out.getvalue())))
        self.assertEqual(rows[0][0], "date")
        self.assertEqual(len(rows), 2)
        header = rows[0]
        record = dict(zip(header, rows[1]))
        self.assertEqual(record["project_path"], "/work/app")
        self.assertEqual(record["session_id"], "s1")
        self.assertAlmostEqual(float(record["cost_usd"]), 12.5)
        self.assertAlmostEqual(float(record["cache_hit_ratio"]), 900 / 1050, places=4)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(
                ["--root", self.root, "--csv", "--no-cache", "--fail-over", "10"]
            )
        self.assertEqual(code, 3)


if __name__ == "__main__":
    unittest.main()
