#!/usr/bin/env python3
"""Experimental Gemini CLI cost ledger and budget guard.

This is a spike, not part of the SpendTab package. It reads Gemini CLI hook JSON on
stdin, prices token usage from a local table, appends a ledger row, and can deny a
tool call when a budget is exceeded. It never stores prompts or responses.

    spike.py after-model   # AfterModel hook: append usage row, silent
    spike.py before-tool   # BeforeTool hook: exit 2 to block
    spike.py report        # human/JSON/CSV report from the ledger
    spike.py init          # print config + hook snippet
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PRICES = os.path.join(HERE, "prices.json")
DEFAULT_CONFIG = "~/.config/spendtab/gemini-guard.json"
DEFAULT_LEDGER = "~/.local/state/spendtab/gemini-usage.jsonl"
MONEY_FIELDS = ("input", "output", "cached", "long_input", "long_output", "long_cached")
DEDUPE_WINDOW_SECONDS = 2.0


def _path(value: Optional[str], default: str) -> str:
    return os.path.expanduser(value or default)


def _finite(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) and number >= 0 else default


def _int(value) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _parse_when(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now().astimezone()


def _day(value) -> str:
    return _parse_when(value).astimezone().date().isoformat()


def load_prices(path: Optional[str] = None) -> Dict:
    target = _path(path, DEFAULT_PRICES)
    with open(target, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict) or not isinstance(raw.get("models"), list):
        raise ValueError("prices file must contain a models array")
    return raw


def _select_tier(entry: Dict, when: datetime) -> Dict:
    tiers = entry.get("tiers")
    if not isinstance(tiers, list) or not tiers:
        return entry
    today = when.astimezone().date().isoformat()
    chosen = None
    for tier in tiers:
        until = tier.get("effective_until")
        if not until or today <= str(until):
            chosen = tier
            break
    merged = dict(entry)
    merged.update(chosen or tiers[-1])
    return merged


def select_rate(prices: Dict, model: str, when: datetime) -> Tuple[Optional[Dict], str]:
    normalized = str(model or "").split("/")[-1].lower()
    best: Optional[Tuple[int, Dict]] = None
    for entry in prices.get("models", []):
        for match in entry.get("match", []):
            needle = str(match).lower()
            if normalized == needle or normalized.startswith(needle):
                if best is None or len(needle) > best[0]:
                    best = (len(needle), entry)
    if best is None:
        return None, normalized or "unknown"
    selected = _select_tier(best[1], when)
    rates = {}
    for key in MONEY_FIELDS:
        if selected.get(key) is not None:
            rates[key] = _finite(selected.get(key))
    if selected.get("long_context_threshold") is not None:
        rates["long_context_threshold"] = _int(selected["long_context_threshold"])
    return rates, str(selected.get("name") or normalized)


def token_breakdown(usage: Dict) -> Dict[str, int]:
    prompt = _int(usage.get("promptTokenCount"))
    cached = min(_int(usage.get("cachedContentTokenCount")), prompt)
    tool = _int(usage.get("toolUsePromptTokenCount"))
    candidates = _int(usage.get("candidatesTokenCount"))
    thoughts = _int(usage.get("thoughtsTokenCount"))
    return {
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "tool_tokens": tool,
        "candidate_tokens": candidates,
        "thought_tokens": thoughts,
        "billable_input_tokens": max(0, prompt - cached) + tool,
        "billable_output_tokens": candidates + thoughts,
    }


def cost_for(model: str, usage: Dict, prices: Dict, when: datetime) -> Tuple[Optional[float], Dict[str, int], bool]:
    tokens = token_breakdown(usage)
    rates, _ = select_rate(prices, model, when)
    if rates is None:
        return None, tokens, False
    long_context = bool(
        rates.get("long_context_threshold")
        and tokens["prompt_tokens"] > rates["long_context_threshold"]
    )
    input_rate = rates.get("long_input") if long_context else rates.get("input")
    output_rate = rates.get("long_output") if long_context else rates.get("output")
    cache_rate = rates.get("long_cached") if long_context else rates.get("cached")
    if input_rate is None or output_rate is None:
        return None, tokens, False
    cache_rate = cache_rate if cache_rate is not None else input_rate
    cost = (
        tokens["billable_input_tokens"] * input_rate
        + tokens["cached_tokens"] * cache_rate
        + tokens["billable_output_tokens"] * output_rate
    ) / 1_000_000.0
    if not math.isfinite(cost):
        return None, tokens, long_context
    return cost, tokens, long_context


def _request_hash(request) -> Optional[str]:
    """Hash the request so duplicate AfterModel fires can be recognized.

    The prompt text is hashed locally; only the short hash is stored.
    """
    if not isinstance(request, dict):
        return None
    try:
        canonical = json.dumps(request, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def event_from_hook(payload: Dict, prices: Dict, now: Optional[datetime] = None) -> Optional[Dict]:
    if not isinstance(payload, dict):
        return None
    response = payload.get("llm_response") or {}
    usage = response.get("usageMetadata") or payload.get("usageMetadata") or {}
    if not isinstance(usage, dict) or not usage:
        return None
    request = payload.get("llm_request") or {}
    model = str(request.get("model") or payload.get("model") or "")
    session_id = str(
        payload.get("session_id") or os.environ.get("GEMINI_SESSION_ID") or ""
    )
    if not session_id:
        return None
    when = _parse_when(payload.get("timestamp") or now)
    cost, tokens, long_context = cost_for(model, usage, prices, when)
    project = str(
        payload.get("cwd") or os.environ.get("GEMINI_CWD") or os.getcwd()
    )
    return {
        "ts": when.isoformat(),
        "day": when.astimezone().date().isoformat(),
        "session_id": session_id,
        "project": project,
        "model": str(model or "unknown").split("/")[-1],
        "request_hash": _request_hash(request),
        "cost_usd": round(cost, 9) if cost is not None else None,
        "priced": cost is not None,
        "long_context": long_context,
        "source": "gemini-after-model",
        **tokens,
    }


def append_event(path: str, event: Dict) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":")) + "\n")


def read_events(path: str) -> List[Dict]:
    events: List[Dict] = []
    try:
        handle = open(path, "r", encoding="utf-8")
    except OSError:
        return events
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            cost = event.get("cost_usd")
            if cost is not None:
                cost = _finite(cost, float("nan"))
                if not math.isfinite(cost):
                    continue
                event["cost_usd"] = cost
            events.append(event)
    return events


def _event_time(event: Dict) -> float:
    try:
        parsed = datetime.fromisoformat(str(event.get("ts", "")).replace("Z", "+00:00"))
        return parsed.timestamp()
    except ValueError:
        return 0.0


def _event_tokens(event: Dict) -> int:
    return sum(
        _int(event.get(key))
        for key in (
            "prompt_tokens",
            "cached_tokens",
            "tool_tokens",
            "candidate_tokens",
            "thought_tokens",
        )
    )


def dedupe_events(events: Iterable[Dict], window: float = DEDUPE_WINDOW_SECONDS) -> List[Dict]:
    """Merge duplicate/partial AfterModel fires for one request.

    Gemini CLI can fire AfterModel more than once for the same model request, with
    cumulative usageMetadata. Keep the event with the largest token total when the
    same session/model/request hash repeats inside the window.
    """
    ordered = sorted(events, key=_event_time)
    result: List[Dict] = []
    last_index: Dict[Tuple, int] = {}
    for event in ordered:
        request_hash = event.get("request_hash")
        key = (
            str(event.get("session_id", "")),
            str(event.get("model", "")),
            request_hash or "legacy",
        )
        index = last_index.get(key)
        if index is not None:
            previous = result[index]
            if abs(_event_time(event) - _event_time(previous)) <= window:
                if _event_tokens(event) >= _event_tokens(previous):
                    result[index] = event
                continue
        last_index[key] = len(result)
        result.append(event)
    return result


@dataclass
class GuardConfig:
    session_budget_usd: float = 5.0
    daily_budget_usd: float = 25.0
    project_daily_budget_usd: float = 15.0
    mode: str = "block"


def load_guard(path: Optional[str] = None) -> Optional[GuardConfig]:
    target = _path(path, DEFAULT_CONFIG)
    try:
        with open(target, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    mode = str(raw.get("mode", "block")).lower()
    if mode not in ("block", "warn"):
        mode = "block"
    return GuardConfig(
        session_budget_usd=_finite(raw.get("session_budget_usd"), 5.0),
        daily_budget_usd=_finite(raw.get("daily_budget_usd"), 25.0),
        project_daily_budget_usd=_finite(raw.get("project_daily_budget_usd"), 15.0),
        mode=mode,
    )


def handle_after_model(payload: Dict, prices_path: Optional[str] = None, ledger_path: Optional[str] = None) -> int:
    try:
        prices = load_prices(prices_path)
        event = event_from_hook(payload, prices)
        if event is None:
            return 0
        append_event(_path(ledger_path, DEFAULT_LEDGER), event)
    except Exception as exc:  # hooks must fail open
        print(f"spendtab-gemini after-model: {exc}", file=sys.stderr)
    return 0


def _money(value: float) -> str:
    return f"${value:,.4f}" if abs(value) < 1 else f"${value:,.2f}"


def evaluate_guard(
    config: GuardConfig,
    events: Iterable[Dict],
    session_id: str,
    project: str,
    now: Optional[datetime] = None,
) -> Tuple[bool, str, Dict[str, float]]:
    day = _day(now or datetime.now().astimezone())
    session_total = 0.0
    day_total = 0.0
    project_total = 0.0
    for event in events:
        cost = event.get("cost_usd")
        if cost is None:
            continue
        cost = _finite(cost, 0.0)
        if str(event.get("session_id")) == session_id:
            session_total += cost
        if str(event.get("day")) == day:
            day_total += cost
            if str(event.get("project")) == project:
                project_total += cost
    reasons = []
    if session_total > config.session_budget_usd:
        reasons.append(
            f"session {_money(session_total)} > {_money(config.session_budget_usd)}"
        )
    if project_total > config.project_daily_budget_usd:
        reasons.append(
            f"project today {_money(project_total)} > {_money(config.project_daily_budget_usd)}"
        )
    if day_total > config.daily_budget_usd:
        reasons.append(
            f"today {_money(day_total)} > {_money(config.daily_budget_usd)}"
        )
    return (not reasons), "; ".join(reasons), {
        "session": session_total,
        "project": project_total,
        "day": day_total,
    }


def handle_before_tool(
    payload: Dict,
    config_path: Optional[str] = None,
    ledger_path: Optional[str] = None,
    dry_run: bool = False,
    now: Optional[datetime] = None,
) -> int:
    config = load_guard(config_path)
    if config is None:
        if dry_run:
            print(json.dumps({"allowed": True, "reason": "no guard config"}, indent=2))
        return 0
    session_id = str(
        payload.get("session_id") or os.environ.get("GEMINI_SESSION_ID") or ""
    )
    project = str(payload.get("cwd") or os.environ.get("GEMINI_CWD") or os.getcwd())
    allowed, reason, totals = evaluate_guard(
        config,
        dedupe_events(read_events(_path(ledger_path, DEFAULT_LEDGER))),
        session_id,
        project,
        now,
    )
    if dry_run:
        print(
            json.dumps(
                {
                    "allowed": allowed or config.mode == "warn",
                    "blocked": (not allowed) and config.mode == "block",
                    "reason": reason or None,
                    "mode": config.mode,
                    "totals": {key: round(value, 6) for key, value in totals.items()},
                },
                indent=2,
            )
        )
        return 0
    if allowed:
        return 0
    message = (
        f"SpendTab Gemini guard: {reason}. "
        "Raise the budget in the config, or start a new session."
    )
    print(message, file=sys.stderr)
    return 2 if config.mode == "block" else 1


def build_report(events: Iterable[Dict], days: int = 7, now: Optional[datetime] = None) -> Dict:
    now = now or datetime.now().astimezone()
    cutoff = (now.astimezone().date() - timedelta(days=max(1, days) - 1)).isoformat()
    selected = [
        e for e in dedupe_events(events) if str(e.get("day", "")) >= cutoff
    ]
    by_day: Dict[str, Dict] = {}
    by_project: Dict[str, Dict] = {}
    by_model: Dict[str, Dict] = {}
    sessions: Dict[str, Dict] = {}
    unpriced = 0
    total = 0.0
    for event in selected:
        cost = event.get("cost_usd")
        priced = cost is not None
        cost = _finite(cost, 0.0) if priced else 0.0
        if not priced:
            unpriced += 1
        total += cost
        for bucket, key in (
            (by_day, str(event.get("day", "unknown"))),
            (by_project, str(event.get("project", "(unknown)"))),
            (by_model, str(event.get("model", "(unknown)"))),
        ):
            entry = bucket.setdefault(key, {"name": key, "cost_usd": 0.0, "events": 0})
            entry["cost_usd"] += cost
            entry["events"] += 1
        session = sessions.setdefault(
            str(event.get("session_id", "")),
            {
                "session_id": str(event.get("session_id", "")),
                "project": str(event.get("project", "")),
                "day": str(event.get("day", "")),
                "cost_usd": 0.0,
                "events": 0,
            },
        )
        session["cost_usd"] += cost
        session["events"] += 1
        session["day"] = str(event.get("day", ""))
    rows = {
        key: sorted(value.values(), key=lambda item: -item["cost_usd"])
        for key, value in (("by_day", by_day), ("by_project", by_project), ("by_model", by_model))
    }
    top = sorted(sessions.values(), key=lambda item: -item["cost_usd"])[:10]
    return {
        "kind": "spendtab_gemini_spike",
        "window": {"start": cutoff, "end": now.astimezone().date().isoformat()},
        "totals": {
            "cost_usd": round(total, 6),
            "events": len(selected),
            "sessions": len(sessions),
            "unpriced_events": unpriced,
        },
        "by_day": rows["by_day"],
        "by_project": rows["by_project"],
        "by_model": rows["by_model"],
        "top_sessions": top,
        "note": "Standard paid-tier list-price estimate. Free-tier usage is $0 in reality.",
    }


def render_report(report: Dict) -> str:
    totals = report["totals"]
    lines = [
        f"SpendTab Gemini spike — {report['window']['start']} → {report['window']['end']}",
        "",
        f"total     ${totals['cost_usd']:,.6f}  (estimate)",
        f"events    {totals['events']} · sessions {totals['sessions']} · "
        f"unpriced {totals['unpriced_events']}",
        "",
    ]

    def section(title: str, rows: List[Dict], limit: int = 10) -> None:
        if not rows:
            return
        lines.append(title)
        top = max((row["cost_usd"] for row in rows), default=0.0) or 1.0
        for row in rows[:limit]:
            filled = max(0, min(12, round(row["cost_usd"] / top * 12)))
            bar = "█" * filled + "░" * (12 - filled)
            lines.append(
                f"  ${row['cost_usd']:>10.4f}  {bar}  {row['name']}  ({row['events']} events)"
            )
        lines.append("")

    section("By day", report["by_day"])
    section("By project", report["by_project"])
    section("By model", report["by_model"])
    if report["top_sessions"]:
        lines.append("Top sessions")
        for row in report["top_sessions"]:
            lines.append(
                f"  ${row['cost_usd']:>10.4f}  {row['day']:<10}  "
                f"{row['project'][:40]}  {row['session_id'][:12]}"
            )
        lines.append("")
    lines.append(report["note"])
    return "\n".join(lines).rstrip() + "\n"


def write_csv(report: Dict) -> None:
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(["dimension", "name", "cost_usd", "events"])
    for dimension in ("by_day", "by_project", "by_model"):
        for row in report[dimension]:
            writer.writerow(
                [dimension.replace("by_", ""), row["name"], f"{row['cost_usd']:.6f}", row["events"]]
            )


def hook_snippet(script_path: str) -> Dict:
    command = f'python3 "{script_path}"'
    return {
        "hooksConfig": {"enabled": True},
        "hooks": {
            "AfterModel": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "name": "spendtab-gemini-cost",
                            "type": "command",
                            "command": command + " after-model",
                            "timeout": 5000,
                        }
                    ],
                }
            ],
            "BeforeTool": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "name": "spendtab-gemini-guard",
                            "type": "command",
                            "command": command + " before-tool",
                            "timeout": 5000,
                        }
                    ],
                }
            ],
        },
    }


def cmd_init(args) -> int:
    config_path = _path(args.config, DEFAULT_CONFIG)
    if args.write_config:
        if os.path.exists(config_path) and not args.force:
            print(f"config exists: {config_path}", file=sys.stderr)
            return 2
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "session_budget_usd": 5.0,
                    "daily_budget_usd": 25.0,
                    "project_daily_budget_usd": 15.0,
                    "mode": "warn",
                },
                handle,
                indent=2,
            )
            handle.write("\n")
        print(f"wrote {config_path}", file=sys.stderr)
    print(json.dumps(hook_snippet(os.path.abspath(__file__)), indent=2))
    print(
        f"\nConfig: {config_path}\nLedger: {_path(args.ledger, DEFAULT_LEDGER)}\n"
        "Set SPENDTAB_GEMINI_PRICES to override the price table.",
        file=sys.stderr,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    after = sub.add_parser("after-model", help="AfterModel hook entry point")
    after.add_argument("--prices", default=None)
    after.add_argument("--ledger", default=None)
    after.set_defaults(func=cmd_after_model)

    before = sub.add_parser("before-tool", help="BeforeTool hook entry point")
    before.add_argument("--config", default=None)
    before.add_argument("--ledger", default=None)
    before.add_argument("--dry-run", action="store_true")
    before.set_defaults(func=cmd_before_tool)

    report = sub.add_parser("report", help="report ledger spend")
    report.add_argument("--ledger", default=None)
    report.add_argument("--days", type=int, default=7)
    report.add_argument("--json", action="store_true")
    report.add_argument("--csv", action="store_true")
    report.set_defaults(func=cmd_report)

    init = sub.add_parser("init", help="print hook config and example budgets")
    init.add_argument("--config", default=None)
    init.add_argument("--ledger", default=None)
    init.add_argument("--write-config", action="store_true")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)
    return parser


def _read_stdin() -> Dict:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def cmd_after_model(args) -> int:
    return handle_after_model(
        _read_stdin(),
        prices_path=args.prices or os.environ.get("SPENDTAB_GEMINI_PRICES"),
        ledger_path=args.ledger or os.environ.get("SPENDTAB_GEMINI_LEDGER"),
    )


def cmd_before_tool(args) -> int:
    return handle_before_tool(
        _read_stdin(),
        config_path=args.config or os.environ.get("SPENDTAB_GEMINI_CONFIG"),
        ledger_path=args.ledger or os.environ.get("SPENDTAB_GEMINI_LEDGER"),
        dry_run=args.dry_run,
    )


def cmd_report(args) -> int:
    events = read_events(
        _path(args.ledger or os.environ.get("SPENDTAB_GEMINI_LEDGER"), DEFAULT_LEDGER)
    )
    report = build_report(events, days=max(1, args.days))
    if args.json:
        print(json.dumps(report, indent=2))
    elif args.csv:
        write_csv(report)
    else:
        sys.stdout.write(render_report(report))
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
