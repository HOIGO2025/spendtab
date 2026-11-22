"""PreToolUse hook entry point.

Wire with:

    spendtab hook pretooluse

The hook reads Claude Code's JSON on stdin. It exits 2 to block a tool call when
a budget is exceeded, 0 otherwise. Any internal failure allows the call.
"""

from __future__ import annotations

import glob
import json
import os
import sys
import urllib.request
from datetime import datetime
from typing import Dict, Optional

from .guard import (
    Event,
    append_event,
    block_message,
    evaluate,
    latest_per_session,
    load_config,
    read_events,
    read_last_cost,
)


def _find_transcript(session_id: str) -> Optional[str]:
    if not session_id:
        return None
    root = os.path.expanduser("~/.claude/projects")
    matches = glob.glob(os.path.join(root, "**", f"{session_id}.jsonl"), recursive=True)
    if not matches:
        return None

    def mtime(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return -1.0

    return max(matches, key=mtime)


def handle_pretooluse(
    payload: Dict,
    config_file: Optional[str] = None,
    ledger_file: Optional[str] = None,
    dry_run: bool = False,
    now: Optional[datetime] = None,
) -> int:
    config = load_config(config_file)
    if config is None:
        # Fail open: a missing config must never brick a session.
        if dry_run:
            print(
                json.dumps(
                    {
                        "allowed": True,
                        "blocked": False,
                        "reason": "no guard config",
                        "config_found": False,
                    },
                    indent=2,
                )
            )
        return 0

    session_id = str(payload.get("session_id") or "")
    project = str(payload.get("cwd") or os.getcwd())
    tool = str(payload.get("tool_name") or "")
    transcript = payload.get("transcript_path") or _find_transcript(session_id)

    session_cost = read_last_cost(str(transcript)) if transcript else None
    if session_cost is None:
        # Unknown spend cannot be enforced per-session; daily totals still apply.
        session_cost = 0.0

    events = read_events(ledger_file)
    decision = evaluate(
        config,
        session_id=session_id,
        project=project,
        session_cost=session_cost,
        events=events,
        now=now,
        config_found=True,
    )

    prior = latest_per_session(events).get(session_id)
    prior_cost = prior.cost_usd if prior else 0.0
    delta = max(0.0, decision.session_cost - prior_cost)
    state = (
        "block"
        if decision.blocked
        else ("warn" if decision.reason is not None else "allow")
    )
    event = Event(
        ts=(now or datetime.now().astimezone()).isoformat(),
        day=(now or datetime.now().astimezone()).astimezone().date().isoformat(),
        session_id=session_id,
        project=project,
        cost_usd=decision.session_cost,
        dev=config.dev,
        tool=tool,
        decision=state,
        delta_usd=delta,
    )
    if not dry_run:
        append_event(event, ledger_file)
        if decision.reason is not None and (
            prior is None or prior.decision != state or prior.day != event.day
        ):
            _notify(config, decision)

    if dry_run:
        print(json.dumps(decision.to_dict(), indent=2))
        return 0

    if decision.blocked:
        print(block_message(decision), file=sys.stderr)
        return 2
    if not decision.allowed:
        print(
            "SpendTab guard warning: " + (decision.reason or "budget exceeded"),
            file=sys.stderr,
        )
    return 0


def _notify(config, decision) -> None:
    """Fire once per state change. Network failure never affects the decision."""
    if not config.webhook_url or config.webhook_on == "never":
        return
    if config.webhook_on == "block" and not decision.blocked:
        return
    if decision.reason is None:
        return
    text = (
        block_message(decision)
        if decision.blocked
        else "SpendTab guard warning: " + decision.reason
    )
    body = json.dumps(
        {"text": text, "dev": config.dev}
    ).encode("utf-8")
    request = urllib.request.Request(
        config.webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=1.5) as response:
            response.read(1)
    except Exception:
        # A webhook must never break a tool call or delay it meaningfully.
        return


def guard_check(
    transcript: Optional[str],
    session_id: str,
    project: str,
    config_file: Optional[str],
    ledger_file: Optional[str],
    as_json: bool = False,
) -> int:
    config = load_config(config_file)
    if config is None:
        print(
            "SpendTab guard: no config found. Run `spendtab guard init` first.",
            file=sys.stderr,
        )
        return 2

    cost = read_last_cost(transcript) if transcript else None
    decision = evaluate(
        config,
        session_id=session_id,
        project=project,
        session_cost=cost or 0.0,
        events=read_events(ledger_file),
    )
    if as_json:
        print(json.dumps(decision.to_dict(), indent=2))
    else:
        from .guard import render_check

        print(render_check(decision))
    return 1 if decision.blocked else 0


SAMPLE_CONFIG = {
    "session_budget_usd": 10.0,
    "daily_budget_usd": 50.0,
    "project_daily_budget_usd": 30.0,
    "dev": "you",
    "dev_daily_budget_usd": 40.0,
    "mode": "warn",
    "project_budgets": {"~/work/client-a": 75.0},
    "webhook_url": None,
    "webhook_on": "block",
}

SETTINGS_SNIPPET = {
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "*",
                "hooks": [
                    {"type": "command", "command": "spendtab hook pretooluse"}
                ],
            }
        ]
    }
}


def guard_init(config_file: Optional[str], force: bool = False) -> int:
    from .guard import config_path

    path = config_file or config_path()
    if os.path.exists(path) and not force:
        print(f"SpendTab guard: config already exists at {path}", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(SAMPLE_CONFIG, handle, indent=2)
        handle.write("\n")
    print(f"Wrote {path}")
    print("\nAdd this to your project's .claude/settings.local.json:\n")
    print(json.dumps(SETTINGS_SNIPPET, indent=2))
    print(
        "\nThen run `spendtab guard check --transcript <path>` to verify, and "
        "`spendtab guard init --force` after editing budgets."
    )
    return 0
