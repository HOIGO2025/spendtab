import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone

from prototype.gemini import spike

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


def usage(**overrides):
    base = {
        "promptTokenCount": 1000,
        "cachedContentTokenCount": 200,
        "candidatesTokenCount": 100,
        "thoughtsTokenCount": 20,
        "toolUsePromptTokenCount": 0,
    }
    base.update(overrides)
    return base


def payload(model="gemini-2.5-pro", token_usage=None, session_id="s1", cwd="/work/app"):
    return {
        "session_id": session_id,
        "cwd": cwd,
        "hook_event_name": "AfterModel",
        "timestamp": "2026-11-23T12:00:00+00:00",
        "llm_request": {
            "model": model,
            "messages": [{"role": "user", "content": "SECRET PROMPT"}],
        },
        "llm_response": {"usageMetadata": token_usage or usage()},
    }


class PricingTests(unittest.TestCase):
    def setUp(self):
        self.prices = spike.load_prices()

    def test_pro_cost_with_cache_and_thoughts(self):
        cost, tokens, long_context = spike.cost_for(
            "gemini-2.5-pro", usage(), self.prices, NOW
        )
        self.assertFalse(long_context)
        self.assertAlmostEqual(cost, 0.002225, places=9)
        self.assertEqual(tokens["billable_input_tokens"], 800)
        self.assertEqual(tokens["billable_output_tokens"], 120)

    def test_pro_long_context_uses_high_tier(self):
        token_usage = usage(
            promptTokenCount=250_000,
            cachedContentTokenCount=0,
            candidatesTokenCount=1_000,
            thoughtsTokenCount=0,
        )
        cost, tokens, long_context = spike.cost_for(
            "gemini-2.5-pro", token_usage, self.prices, NOW
        )
        self.assertTrue(long_context)
        self.assertAlmostEqual(cost, 0.64, places=9)
        self.assertEqual(tokens["billable_input_tokens"], 250_000)

    def test_flash_promo_and_2027_price(self):
        token_usage = usage(
            promptTokenCount=1_000_000,
            cachedContentTokenCount=0,
            candidatesTokenCount=1_000_000,
            thoughtsTokenCount=0,
        )
        promo, _, _ = spike.cost_for(
            "gemini-3.8-flash", token_usage, self.prices, NOW
        )
        later, _, _ = spike.cost_for(
            "gemini-3.8-flash",
            token_usage,
            self.prices,
            datetime(2027, 1, 2, tzinfo=timezone.utc),
        )
        self.assertAlmostEqual(promo, 4.5, places=9)
        self.assertAlmostEqual(later, 9.0, places=9)

    def test_unknown_model_is_unpriced(self):
        cost, tokens, _ = spike.cost_for("mystery-model", usage(), self.prices, NOW)
        self.assertIsNone(cost)
        self.assertEqual(tokens["prompt_tokens"], 1000)

    def test_tool_tokens_are_billed_as_input(self):
        cost, tokens, _ = spike.cost_for(
            "gemini-2.5-flash",
            usage(promptTokenCount=100, cachedContentTokenCount=0, toolUsePromptTokenCount=50),
            self.prices,
            NOW,
        )
        self.assertEqual(tokens["billable_input_tokens"], 150)
        self.assertAlmostEqual(cost, (150 * 0.30 + 120 * 2.50) / 1_000_000, places=9)


class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = os.path.join(self.tmp.name, "usage.jsonl")
        self.config = os.path.join(self.tmp.name, "guard.json")
        self.prices = spike.load_prices()

    def after(self, data):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = spike.handle_after_model(data, ledger_path=self.ledger)
        return code, out.getvalue(), err.getvalue()

    def before(self, data, dry_run=False):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = spike.handle_before_tool(
                data,
                config_path=self.config,
                ledger_path=self.ledger,
                dry_run=dry_run,
                now=NOW,
            )
        return code, out.getvalue(), err.getvalue()

    def write_config(self, **overrides):
        raw = {
            "session_budget_usd": 5,
            "daily_budget_usd": 25,
            "project_daily_budget_usd": 15,
            "mode": "block",
        }
        raw.update(overrides)
        with open(self.config, "w", encoding="utf-8") as handle:
            json.dump(raw, handle)

    def test_after_model_is_silent_and_stores_no_prompt(self):
        code, out, err = self.after(payload())
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")
        with open(self.ledger, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("SECRET PROMPT", text)
        event = json.loads(text.strip())
        self.assertEqual(event["session_id"], "s1")
        self.assertEqual(event["model"], "gemini-2.5-pro")
        self.assertAlmostEqual(event["cost_usd"], 0.002225, places=9)

    def test_malformed_input_fails_open(self):
        code, out, err = self.after({"session_id": "s1", "llm_response": {}})
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertFalse(os.path.exists(self.ledger))

    def test_before_tool_allows_under_budget(self):
        self.write_config()
        self.after(payload())
        code, out, err = self.before({"session_id": "s1", "cwd": "/work/app"})
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_before_tool_blocks_over_session_budget(self):
        self.write_config(session_budget_usd=0.001)
        self.after(payload())
        code, out, err = self.before({"session_id": "s1", "cwd": "/work/app"})
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("session", err)

    def test_before_tool_warn_mode_returns_one(self):
        self.write_config(session_budget_usd=0.001, mode="warn")
        self.after(payload())
        code, _, err = self.before({"session_id": "s1", "cwd": "/work/app"})
        self.assertEqual(code, 1)
        self.assertIn("session", err)

    def test_before_tool_dry_run_prints_decision(self):
        self.write_config(session_budget_usd=0.001)
        self.after(payload())
        code, out, _ = self.before(
            {"session_id": "s1", "cwd": "/work/app"}, dry_run=True
        )
        self.assertEqual(code, 0)
        decision = json.loads(out)
        self.assertTrue(decision["blocked"])
        self.assertIn("session", decision["reason"])

    def test_missing_config_fails_open(self):
        self.after(payload())
        code, out, err = self.before({"session_id": "s1", "cwd": "/work/app"})
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_project_budget_only_counts_same_project(self):
        self.write_config(
            session_budget_usd=100,
            daily_budget_usd=100,
            project_daily_budget_usd=0.001,
        )
        self.after(payload())
        code, _, _ = self.before({"session_id": "other", "cwd": "/work/other"})
        self.assertEqual(code, 0)
        code, _, err = self.before({"session_id": "other", "cwd": "/work/app"})
        self.assertEqual(code, 2)
        self.assertIn("project", err)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = os.path.join(self.tmp.name, "usage.jsonl")
        spike.handle_after_model(payload(), ledger_path=self.ledger)
        spike.handle_after_model(
            payload(model="unknown-model", session_id="s2", cwd="/work/other"),
            ledger_path=self.ledger,
        )

    def test_report_totals_and_unpriced(self):
        report = spike.build_report(spike.read_events(self.ledger), now=NOW)
        self.assertEqual(report["totals"]["events"], 2)
        self.assertEqual(report["totals"]["sessions"], 2)
        self.assertEqual(report["totals"]["unpriced_events"], 1)
        self.assertAlmostEqual(report["totals"]["cost_usd"], 0.002225, places=9)
        json.dumps(report, allow_nan=False)
        text = spike.render_report(report)
        self.assertIn("SpendTab Gemini spike", text)
        self.assertIn("unpriced 1", text)

    def test_report_window_filters_old_rows(self):
        reports = spike.build_report(spike.read_events(self.ledger), days=1, now=NOW)
        self.assertEqual(reports["totals"]["events"], 2)

    def test_init_snippet_has_both_hooks(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = spike.main(["init"])
        self.assertEqual(code, 0)
        config = json.loads(out.getvalue())
        self.assertIn("AfterModel", config["hooks"])
        self.assertIn("BeforeTool", config["hooks"])
        self.assertIn("after-model", config["hooks"]["AfterModel"][0]["hooks"][0]["command"])
        self.assertIn("before-tool", config["hooks"]["BeforeTool"][0]["hooks"][0]["command"])


if __name__ == "__main__":
    unittest.main()
