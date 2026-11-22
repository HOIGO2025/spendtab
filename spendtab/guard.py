"""Budget enforcement for Claude Code tool calls.

The report tells you what was spent. The guard stops the next tool call when a
session, project, or day crosses its budget. It is designed to be safe to wire
into a PreToolUse hook: missing or broken state fails open, never blocking work.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlsplit

CONFIG_ENV = "SPENDTAB_GUARD_CONFIG"
STATE_ENV = "SPENDTAB_STATE_DIR"
TAIL_BYTES = 8 * 1024 * 1024
LEDGER_MAX_BYTES = 8 * 1024 * 1024
LEDGER_KEEP_DAYS = 30


@dataclass
class Config:
    session_budget_usd: float = 5.0
    daily_budget_usd: float = 25.0
    project_daily_budget_usd: float = 15.0
    dev: Optional[str] = None
    mode: str = "block"  # "block" | "warn"
    project_budgets: Dict[str, float] = field(default_factory=dict)
    dev_daily_budget_usd: Optional[float] = None
    webhook_url: Optional[str] = None
    webhook_on: str = "block"  # "block" | "always" | "never"

    @classmethod
    def from_dict(cls, raw: Dict) -> "Config":
        budgets = raw.get("project_budgets") or {}
        if not isinstance(budgets, dict):
            budgets = {}
        mode = str(raw.get("mode", "block")).lower()
        if mode not in ("block", "warn"):
            mode = "block"
        webhook_on = str(raw.get("webhook_on", "block")).lower()
        if webhook_on not in ("block", "always", "never"):
            webhook_on = "block"
        return cls(
            session_budget_usd=_positive(raw.get("session_budget_usd"), 5.0),
            daily_budget_usd=_positive(raw.get("daily_budget_usd"), 25.0),
            project_daily_budget_usd=_positive(
                raw.get("project_daily_budget_usd"), 15.0
            ),
            dev=raw.get("dev"),
            mode=mode,
            project_budgets={
                str(key): _positive(value, 0.0)
                for key, value in budgets.items()
                if _positive(value, 0.0) > 0
            },
            webhook_url=_webhook_url(raw.get("webhook_url")),
            webhook_on=webhook_on,
            dev_daily_budget_usd=_optional_positive(
                raw.get("dev_daily_budget_usd")
            ),
        )

    def budget_for_project(self, project: str) -> float:
        for prefix, budget in self.project_budgets.items():
            expanded = os.path.expanduser(prefix)
            if project == expanded or project.startswith(expanded.rstrip("/") + "/"):
                return budget
        return self.project_daily_budget_usd


def _positive(value, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number) or number <= 0:
        return default
    return number


def _optional_positive(value) -> Optional[float]:
    if value is None:
        return None
    number = _positive(value, 0.0)
    return number if number > 0 else None


def _webhook_url(value) -> Optional[str]:
    """Only https, or http on loopback, is accepted. Never send elsewhere."""
    if not isinstance(value, str) or not value.strip():
        return None
    url = value.strip()
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.username or parts.password:
        return None
    if parts.scheme == "https" and hostname:
        return url
    if parts.scheme == "http" and hostname in ("127.0.0.1", "localhost", "::1") and port:
        return url
    return None


def config_path() -> str:
    return os.path.expanduser(
        os.environ.get(CONFIG_ENV, "~/.config/spendtab/guard.json")
    )


def ledger_path() -> str:
    return os.path.expanduser(
        os.environ.get(STATE_ENV, "~/.local/state/spendtab") + "/ledger.jsonl"
    )


def load_config(path: Optional[str] = None) -> Optional[Config]:
    path = path or config_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            return None
        return Config.from_dict(raw)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return None


def read_last_cost(path: str, tail_bytes: int = TAIL_BYTES) -> Optional[float]:
    """Find the last ``cost-state`` snapshot by scanning the file backwards.

    Cost snapshots are written near the end of a turn, so a bounded tail scan
    keeps hook latency low on multi-hundred-MB transcripts.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size <= 0:
        return None
    start = max(0, size - tail_bytes)
    try:
        with open(path, "rb") as handle:
            handle.seek(start)
            data = handle.read()
    except OSError:
        return None

    lines = data.split(b"\n")
    # A partial first fragment from the middle of a line simply fails to parse.
    for raw in reversed(lines):
        if b'"cost-state"' not in raw:
            continue
        try:
            record = json.loads(raw.decode("utf-8", errors="ignore"))
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("type") == "cost-state":
            try:
                value = float(record.get("totalCostUSD", 0.0) or 0.0)
            except (TypeError, ValueError):
                return None
            return value if math.isfinite(value) and value >= 0 else None
    return None


def _today_key(now: Optional[datetime] = None) -> str:
    return (now or datetime.now().astimezone()).astimezone().date().isoformat()


@dataclass
class Event:
    ts: str
    day: str
    session_id: str
    project: str
    cost_usd: float
    dev: Optional[str] = None
    tool: Optional[str] = None
    decision: str = "allow"
    delta_usd: Optional[float] = None


def read_events(path: Optional[str] = None) -> List[Event]:
    path = path or ledger_path()
    events: List[Event] = []
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
                raw = json.loads(line)
            except ValueError:
                continue
            if not isinstance(raw, dict):
                continue
            try:
                cost = float(raw.get("cost_usd", 0.0) or 0.0)
                if not math.isfinite(cost) or cost < 0:
                    continue
                delta: Optional[float] = None
                if raw.get("delta_usd") is not None:
                    delta = float(raw["delta_usd"])
                    if not math.isfinite(delta) or delta < 0:
                        delta = 0.0
                dev = raw.get("dev")
                if dev is not None:
                    dev = str(dev)
                events.append(
                    Event(
                        ts=str(raw.get("ts", "")),
                        day=str(raw.get("day", "")),
                        session_id=str(raw.get("session_id", "")),
                        project=str(raw.get("project", "")),
                        cost_usd=cost,
                        dev=dev,
                        tool=raw.get("tool"),
                        decision=str(raw.get("decision", "allow")),
                        delta_usd=delta,
                    )
                )
            except (TypeError, ValueError):
                continue
    return events


def append_event(event: Event, path: Optional[str] = None) -> None:
    path = path or ledger_path()
    try:
        _compact_if_large(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.__dict__, separators=(",", ":")) + "\n")
    except OSError:
        pass


def _compact_if_large(path: str) -> None:
    try:
        if os.path.getsize(path) <= LEDGER_MAX_BYTES:
            return
    except OSError:
        return
    compact_ledger(path)


def compact_ledger(
    path: Optional[str] = None,
    keep_days: int = LEDGER_KEEP_DAYS,
    now: Optional[datetime] = None,
) -> int:
    """Drop ledger rows older than ``keep_days``. Returns rows kept."""
    path = path or ledger_path()
    cutoff = (now or datetime.now().astimezone()).astimezone().date() - timedelta(
        days=keep_days
    )
    cutoff_key = cutoff.isoformat()
    try:
        events = read_events(path)
    except OSError:
        return 0
    kept = [event for event in events if event.day >= cutoff_key]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as handle:
            for event in kept:
                handle.write(json.dumps(event.__dict__, separators=(",", ":")) + "\n")
        os.replace(tmp, path)
    except OSError:
        return len(events)
    return len(kept)


def latest_per_session(events: Iterable[Event]) -> Dict[str, Event]:
    latest: Dict[str, Event] = {}
    for event in events:
        current = latest.get(event.session_id)
        if current is None or event.cost_usd >= current.cost_usd:
            latest[event.session_id] = event
    return latest


def _ts_key(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def event_deltas(events: Iterable[Event]) -> List[tuple]:
    """Attribute only the increase in each session's cumulative cost.

    ``delta_usd`` is written by the hook; older rows fall back to comparing
    cumulative snapshots. Sorting by local day, then by absolute timestamp,
    keeps snapshots in order even when clock offsets differ.
    """
    ordered = sorted(events, key=lambda e: (e.day, _ts_key(e.ts)))
    previous: Dict[str, float] = {}
    out: List[tuple] = []
    for event in ordered:
        prior = previous.get(event.session_id)
        if event.delta_usd is not None:
            delta = max(0.0, event.delta_usd)
        elif prior is None:
            delta = event.cost_usd
        else:
            delta = max(0.0, event.cost_usd - prior)
        previous[event.session_id] = max(prior or 0.0, event.cost_usd)
        out.append((event, delta))
    return out


@dataclass
class Decision:
    allowed: bool
    blocked: bool
    reason: Optional[str]
    session_cost: float
    session_budget: float
    project: str
    project_today: float
    project_budget: float
    day_today: float
    day_budget: float
    mode: str
    dev: Optional[str] = None
    dev_today: float = 0.0
    dev_budget: Optional[float] = None
    config_found: bool = True

    def to_dict(self) -> Dict[str, object]:
        return {
            "allowed": self.allowed,
            "blocked": self.blocked,
            "reason": self.reason,
            "session_cost_usd": round(self.session_cost, 6),
            "session_budget_usd": self.session_budget,
            "project": self.project,
            "project_today_usd": round(self.project_today, 6),
            "project_budget_usd": self.project_budget,
            "day_today_usd": round(self.day_today, 6),
            "day_budget_usd": self.day_budget,
            "dev": self.dev,
            "dev_today_usd": round(self.dev_today, 6),
            "dev_budget_usd": self.dev_budget,
            "mode": self.mode,
            "config_found": self.config_found,
        }


def evaluate(
    config: Config,
    session_id: str,
    project: str,
    session_cost: float,
    events: Iterable[Event],
    now: Optional[datetime] = None,
    config_found: bool = True,
) -> Decision:
    if not math.isfinite(session_cost) or session_cost < 0:
        session_cost = 0.0
    day = _today_key(now)

    # Budgets are charged on today's *increase*, not the session's lifetime
    # total. A session resumed from yesterday must not re-charge yesterday.
    buckets = event_deltas(events)
    todays = [(event, delta) for event, delta in buckets if event.day == day]
    prior = latest_per_session(events).get(session_id)
    baseline = prior.cost_usd if prior else 0.0
    current_delta = max(0.0, session_cost - baseline)

    daily_total = sum(delta for _, delta in todays) + current_delta
    project_total = (
        sum(
            delta
            for event, delta in todays
            if _same_project(event.project, project)
        )
        + current_delta
    )

    session_budget = config.session_budget_usd
    project_budget = config.budget_for_project(project)
    day_budget = config.daily_budget_usd

    dev_budget = config.dev_daily_budget_usd if config.dev else None
    if dev_budget is not None:
        dev_total = (
            sum(delta for event, delta in todays if event.dev == config.dev)
            + current_delta
        )
    else:
        dev_total = 0.0

    reasons: List[str] = []
    if session_cost > session_budget:
        reasons.append(
            f"session ${session_cost:.2f} > session budget ${session_budget:.2f}"
        )
    if project_total > project_budget:
        reasons.append(
            f"project today ${project_total:.2f} > project budget ${project_budget:.2f}"
        )
    if daily_total > day_budget:
        reasons.append(
            f"all projects today ${daily_total:.2f} > daily budget ${day_budget:.2f}"
        )
    if dev_budget is not None and dev_total > dev_budget:
        reasons.append(
            f"dev {config.dev} today ${dev_total:.2f} > dev budget ${dev_budget:.2f}"
        )

    violated = bool(reasons)
    if not violated:
        return Decision(
            allowed=True,
            blocked=False,
            reason=None,
            session_cost=session_cost,
            session_budget=session_budget,
            project=project,
            project_today=project_total,
            project_budget=project_budget,
            day_today=daily_total,
            day_budget=day_budget,
            mode=config.mode,
            dev=config.dev,
            dev_today=dev_total,
            dev_budget=dev_budget,
            config_found=config_found,
        )

    return Decision(
        allowed=config.mode == "warn",
        blocked=config.mode != "warn",
        reason="; ".join(reasons),
        session_cost=session_cost,
        session_budget=session_budget,
        project=project,
        project_today=project_total,
        project_budget=project_budget,
        day_today=daily_total,
        day_budget=day_budget,
        mode=config.mode,
        dev=config.dev,
        dev_today=dev_total,
        dev_budget=dev_budget,
        config_found=config_found,
    )


def _same_project(a: str, b: str) -> bool:
    return os.path.normpath(os.path.expanduser(a)) == os.path.normpath(
        os.path.expanduser(b)
    )


def block_message(decision: Decision) -> str:
    return (
        "SpendTab guard: budget exceeded — "
        f"{decision.reason}. "
        "Raise the limit in the guard config, or start a new session with /clear. "
        "No tool call was run."
    )


def render_check(decision: Decision) -> str:
    if decision.blocked:
        state = "BLOCK"
    elif decision.reason:
        state = "WARN"
    else:
        state = "ALLOW"
    lines = [
        f"SpendTab guard: {state}",
        f"session   ${decision.session_cost:,.2f} / ${decision.session_budget:,.2f}",
        f"project   ${decision.project_today:,.2f} / ${decision.project_budget:,.2f}  ({decision.project})",
        f"today     ${decision.day_today:,.2f} / ${decision.day_budget:,.2f}",
    ]
    if decision.dev and decision.dev_budget is not None:
        lines.append(
            f"dev       ${decision.dev_today:,.2f} / ${decision.dev_budget:,.2f}  ({decision.dev})"
        )
    if decision.reason:
        lines.append(f"reason    {decision.reason}")
    return "\n".join(lines)
