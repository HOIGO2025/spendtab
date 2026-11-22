import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tokentab import parse as parse_module
from tokentab.analyze import build_report, filter_sessions, parse_since
from tokentab.cli import main
from tokentab.parse import decode_project_dir, load_sessions
from tokentab.report import render


def model(**overrides):
    base = {
        "inputTokens": 0,
        "outputTokens": 0,
        "thinkingTokens": 0,
        "cacheReadInputTokens": 0,
        "cacheCreationInputTokens": 0,
        "cacheCreationEphemeral5mInputTokens": 0,
        "cacheCreationEphemeral1hInputTokens": 0,
        "webSearchRequests": 0,
        "costUSD": 0.0,
    }
    base.update(overrides)
    return base


def cost_state(session_id, cost, start_ms, models=None, unknown=False, api_ms=1000):
    return {
        "type": "cost-state",
        "sessionId": session_id,
        "totalCostUSD": cost,
        "totalAPIDuration": api_ms,
        "totalDuration": 5000,
        "totalToolDuration": 100,
        "totalLinesAdded": 1,
        "totalLinesRemoved": 2,
        "startTime": start_ms,
        "hasUnknownModelCost": unknown,
        "modelUsage": models or {},
    }


def assistant(session_id):
    return {"type": "assistant", "sessionId": session_id, "message": {"usage": {}}}


def write_file(root, project, filename, records):
    directory = os.path.join(root, project)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, filename)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            if isinstance(record, str):
                handle.write(record + "\n")
            else:
                handle.write(json.dumps(record) + "\n")
    return path


def days_ago_ms(days):
    return int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "projects")
        os.makedirs(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_last_cost_snapshot_wins_and_malformed_lines_skipped(self):
        write_file(
            self.root,
            "-home-me-demo",
            "abc.jsonl",
            [
                assistant("s1"),
                cost_state("s1", 1.0, days_ago_ms(1)),
                "{not json",
                cost_state("s1", 2.5, days_ago_ms(1)),
            ],
        )
        sessions = load_sessions(self.root)
        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        self.assertEqual(session.session_id, "s1")
        self.assertAlmostEqual(session.cost_usd, 2.5)
        self.assertEqual(session.snapshot_count, 2)
        self.assertEqual(session.malformed_lines, 1)
        self.assertEqual(session.assistant_messages, 1)
        self.assertEqual(session.project_path, "/home/me/demo")
        self.assertTrue(session.priced)

    def test_unpriced_session_is_kept_separately(self):
        write_file(
            self.root,
            "-home-me-two",
            "mix.jsonl",
            [
                cost_state("priced", 4.0, days_ago_ms(2)),
                assistant("priced"),
                assistant("unpriced"),
                assistant("unpriced"),
            ],
        )
        sessions = {s.session_id: s for s in load_sessions(self.root)}
        self.assertEqual(set(sessions), {"priced", "unpriced"})
        self.assertTrue(sessions["priced"].priced)
        self.assertFalse(sessions["unpriced"].priced)
        self.assertEqual(sessions["unpriced"].assistant_messages, 2)
        self.assertIsNone(sessions["unpriced"].cost_usd)

    def test_sessions_split_by_session_id_within_file(self):
        write_file(
            self.root,
            "-home-me-split",
            "multi.jsonl",
            [
                cost_state("a", 3.0, days_ago_ms(3)),
                cost_state("b", 7.0, days_ago_ms(3)),
            ],
        )
        costs = {s.session_id: s.cost_usd for s in load_sessions(self.root)}
        self.assertEqual(costs, {"a": 3.0, "b": 7.0})

    def test_decode_project_dir(self):
        self.assertEqual(decode_project_dir("-Users-demo-app"), "/Users/demo/app")
        self.assertIsNone(decode_project_dir("plain-dir"))

    def test_cache_roundtrip_and_invalidation(self):
        path = write_file(
            self.root,
            "-home-me-cache",
            "cached.jsonl",
            [cost_state("c1", 1.0, days_ago_ms(1))],
        )
        cache = os.path.join(self.tmp.name, "cache.json")
        first = load_sessions(self.root, cache_path=cache).copy()
        self.assertTrue(os.path.exists(cache))

        second = load_sessions(self.root, cache_path=cache).copy()
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertAlmostEqual(second[0].cost_usd, 1.0)

        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(cost_state("c1", 9.0, days_ago_ms(1))) + "\n")
        third = load_sessions(self.root, cache_path=cache).copy()
        self.assertAlmostEqual(third[0].cost_usd, 9.0)

    def test_corrupt_cache_is_ignored(self):
        write_file(
            self.root,
            "-home-me-badcache",
            "x.jsonl",
            [cost_state("x", 2.0, days_ago_ms(1))],
        )
        cache = os.path.join(self.tmp.name, "bad.json")
        with open(cache, "w", encoding="utf-8") as handle:
            handle.write("{broken")
        sessions = load_sessions(self.root, cache_path=cache)
        self.assertEqual(len(sessions), 1)
        self.assertAlmostEqual(sessions[0].cost_usd, 2.0)

    def test_malformed_cost_state_does_not_crash(self):
        write_file(
            self.root,
            "-home-me-badcost",
            "bad.jsonl",
            [
                cost_state("s", "NaN", days_ago_ms(1)),
                cost_state("s", 3.0, days_ago_ms(1), models="not-an-object"),
                cost_state("s", 2.0, days_ago_ms(1)),
            ],
        )
        sessions = load_sessions(self.root, use_cache=False)
        self.assertEqual(len(sessions), 1)
        self.assertAlmostEqual(sessions[0].cost_usd, 2.0)
        self.assertGreaterEqual(sessions[0].malformed_lines, 2)

    def test_stale_cache_with_preserved_mtime_is_detected(self):
        path = write_file(
            self.root,
            "-home-me-stale",
            "s.jsonl",
            [cost_state("s", 1.0, days_ago_ms(1))],
        )
        cache = os.path.join(self.tmp.name, "stale.json")
        first = load_sessions(self.root, cache_path=cache)
        self.assertAlmostEqual(first[0].cost_usd, 1.0)

        stat = os.stat(path)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(cost_state("s", 9.9, days_ago_ms(1))) + "\n")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

        second = load_sessions(self.root, cache_path=cache)
        self.assertAlmostEqual(second[0].cost_usd, 9.9)

    def test_duplicate_session_across_files_counted_once(self):
        write_file(
            self.root,
            "-home-me-dup",
            "a.jsonl",
            [cost_state("dup", 5.0, days_ago_ms(1))],
        )
        write_file(
            self.root,
            "-home-me-dup",
            "b.jsonl",
            [cost_state("dup", 7.0, days_ago_ms(1))],
        )
        sessions = load_sessions(self.root, use_cache=False)
        self.assertEqual(len(sessions), 1)
        self.assertAlmostEqual(sessions[0].cost_usd, 7.0)

    def test_file_appended_during_parse_is_not_cached(self):
        path = write_file(
            self.root,
            "-home-me-race",
            "race.jsonl",
            [cost_state("s", 1.0, days_ago_ms(1))],
        )
        cache = os.path.join(self.tmp.name, "race.json")
        original = parse_module._load_file

        def racing(transcript_path):
            result = original(transcript_path)
            with open(transcript_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(cost_state("s", 9.0, days_ago_ms(1))) + "\n")
            return result

        with patch.object(parse_module, "_load_file", side_effect=racing):
            load_sessions(self.root, cache_path=cache)
        self.assertTrue(os.path.exists(path))
        second = load_sessions(self.root, cache_path=cache)
        self.assertAlmostEqual(second[0].cost_usd, 9.0)

    def test_wrong_shape_cache_entry_is_reparsed(self):
        write_file(
            self.root,
            "-home-me-shape",
            "s.jsonl",
            [cost_state("s", 4.0, days_ago_ms(1))],
        )
        cache = os.path.join(self.tmp.name, "shape.json")
        load_sessions(self.root, cache_path=cache)
        with open(cache, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        for entry in payload["files"].values():
            entry["sessions"] = [None]
        with open(cache, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        sessions = load_sessions(self.root, cache_path=cache)
        self.assertEqual(len(sessions), 1)
        self.assertAlmostEqual(sessions[0].cost_usd, 4.0)


class FilterTests(unittest.TestCase):
    def test_parse_since_relative_and_date(self):
        cutoff = parse_since("7d")
        self.assertIsNotNone(cutoff)
        self.assertAlmostEqual(
            (datetime.now(timezone.utc) - cutoff).total_seconds() / 86400, 7, places=2
        )
        dated = parse_since("2026-01-02")
        self.assertEqual(dated.date().isoformat(), "2026-01-02")
        spaced = parse_since("2026-01-02 ")
        self.assertEqual(spaced.date().isoformat(), "2026-01-02")

    def test_parse_since_rejects_garbage(self):
        with self.assertRaises(ValueError):
            parse_since("soon")

    def test_filter_by_project_and_since(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "projects")
        os.makedirs(root)
        write_file(root, "-home-me-alpha", "a.jsonl", [cost_state("a", 1, days_ago_ms(1))])
        write_file(root, "-home-me-beta", "b.jsonl", [cost_state("b", 2, days_ago_ms(40))])
        sessions = load_sessions(root)

        recent = filter_sessions(sessions, since=parse_since("7d"))
        self.assertEqual([s.session_id for s in recent], ["a"])

        alpha = filter_sessions(sessions, project="alpha")
        self.assertEqual([s.session_id for s in alpha], ["a"])

        by_path = filter_sessions(sessions, project="/home/me/beta")
        self.assertEqual([s.session_id for s in by_path], ["b"])


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "projects")
        os.makedirs(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_totals_groups_and_run_rate(self):
        write_file(
            self.root,
            "-home-me-alpha",
            "one.jsonl",
            [
                cost_state(
                    "one",
                    100.0,
                    days_ago_ms(1),
                    {"sonnet": model(inputTokens=1000, outputTokens=100, cacheReadInputTokens=9000, cacheCreationInputTokens=1000, costUSD=100.0)},
                )
            ],
        )
        write_file(
            self.root,
            "-home-me-alpha",
            "two.jsonl",
            [
                cost_state(
                    "two",
                    50.0,
                    days_ago_ms(2),
                    {"sonnet": model(inputTokens=500, outputTokens=50, cacheReadInputTokens=4500, cacheCreationInputTokens=500, costUSD=50.0)},
                )
            ],
        )
        write_file(
            self.root,
            "-home-me-beta",
            "three.jsonl",
            [
                cost_state(
                    "three",
                    10.0,
                    days_ago_ms(3),
                    {"haiku": model(inputTokens=10, outputTokens=5, cacheReadInputTokens=90, cacheCreationInputTokens=10, costUSD=10.0)},
                )
            ],
        )
        sessions = load_sessions(self.root)
        since = datetime.now(timezone.utc) - timedelta(days=10)
        report = build_report(sessions, root=self.root, since=since, top=2)

        self.assertEqual(report.sessions_total, 3)
        self.assertAlmostEqual(report.total_cost_usd, 160.0)
        self.assertEqual(report.by_project[0].project, "-home-me-alpha")
        self.assertAlmostEqual(report.by_project[0].cost_usd, 150.0)
        self.assertEqual(report.by_project[0].sessions, 2)
        self.assertEqual(report.by_project[1].project, "-home-me-beta")

        models = {m.model: m for m in report.by_model}
        self.assertAlmostEqual(models["sonnet"].cost_usd, 150.0)
        self.assertEqual(models["sonnet"].cache_read_tokens, 13500)
        self.assertEqual(len(report.top_sessions), 2)
        self.assertAlmostEqual(report.top_sessions[0].cost_usd, 100.0)

        self.assertIsNotNone(report.run_rate_usd)
        self.assertAlmostEqual(report.run_rate_usd, 480.0, places=2)

        payload = report.to_dict(top=2)
        self.assertEqual(payload["totals"]["sessions"], 3)
        self.assertAlmostEqual(payload["totals"]["cost_usd"], 160.0)
        self.assertEqual(len(payload["top_sessions"]), 2)

    def test_findings_fire_on_waste_patterns(self):
        write_file(
            self.root,
            "-home-me-waste",
            "bad.jsonl",
            [
                cost_state(
                    "bad",
                    120.0,
                    days_ago_ms(1),
                    {
                        "mystery": model(
                            inputTokens=10_000,
                            cacheReadInputTokens=20_000,
                            cacheCreationInputTokens=100_000,
                            cacheCreationEphemeral1hInputTokens=100_000,
                            costUSD=120.0,
                        )
                    },
                    unknown=True,
                ),
                assistant("unpriced"),
            ],
        )
        report = build_report(load_sessions(self.root), root=self.root)
        codes = {f.code for f in report.findings}

        self.assertIn("low_cache_hit", codes)
        self.assertIn("stale_1h_cache", codes)
        self.assertIn("unknown_model_cost", codes)
        self.assertIn("unpriced_sessions", codes)
        self.assertIn("cost_concentration", codes)
        self.assertEqual(report.findings[0].severity, "warn")

    def test_nan_cost_is_not_emitted_as_json(self):
        write_file(
            self.root,
            "-home-me-nan",
            "nan.jsonl",
            [cost_state("nan", "NaN", days_ago_ms(1))],
        )
        report = build_report(load_sessions(self.root, use_cache=False), root=self.root)
        payload = report.to_dict()
        json.dumps(payload, allow_nan=False)
        self.assertEqual(report.sessions_priced, 0)

    def test_healthy_session_has_no_findings(self):
        write_file(
            self.root,
            "-home-me-good",
            "good.jsonl",
            [
                cost_state(
                    "good",
                    2.0,
                    days_ago_ms(1),
                    {
                        "sonnet": model(
                            cacheReadInputTokens=900_000,
                            cacheCreationInputTokens=10_000,
                            costUSD=2.0,
                        )
                    },
                )
            ],
        )
        report = build_report(load_sessions(self.root), root=self.root)
        self.assertEqual(report.findings, [])
        text = render(report, color=False)
        self.assertIn("No spend anomalies detected.", text)
        self.assertNotIn("\033[", text)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "projects")
        os.makedirs(self.root)
        write_file(
            self.root,
            "-home-me-cli",
            "cli.jsonl",
            [cost_state("cli", 12.5, days_ago_ms(1), {"sonnet": model(costUSD=12.5)})],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_json_output(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--root", self.root, "--json", "--no-color", "--no-cache"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["tool"], "tokentab")
        self.assertAlmostEqual(payload["totals"]["cost_usd"], 12.5)
        self.assertEqual(payload["by_project"][0]["path"], "/home/me/cli")

    def test_text_output_has_no_ansi_when_disabled(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--root", self.root, "--no-color", "--no-cache"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("TokenTab", text)
        self.assertIn("$12.50", text)
        self.assertNotIn("\033[", text)

    def test_fail_over_exit_code(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--root", self.root, "--json", "--fail-over", "10", "--no-cache"])
        self.assertEqual(code, 3)
        self.assertIn("exceeds --fail-over", err.getvalue())

    def test_missing_root(self):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = main(["--root", os.path.join(self.tmp.name, "nope"), "--no-cache"])
        self.assertEqual(code, 2)
        self.assertIn("transcript root not found", err.getvalue())


if __name__ == "__main__":
    unittest.main()
