"""Ledger reporting: incremental spend by developer, project, and day."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional

from .guard import Event, event_deltas


@dataclass
class IncrementalEvent:
    event: Event
    delta_usd: float
    dev: str
    project: str
    day: str


def incremental_events(events: Iterable[Event]) -> List[IncrementalEvent]:
    """Turn cumulative snapshots into per-event deltas."""
    out: List[IncrementalEvent] = []
    for event, delta in event_deltas(events):
        if delta <= 0:
            continue
        out.append(
            IncrementalEvent(
                event=event,
                delta_usd=delta,
                dev=event.dev or "(unlabeled)",
                project=event.project or "(unknown)",
                day=event.day or "unknown",
            )
        )
    return out


@dataclass
class GroupSpend:
    name: str
    cost_usd: float = 0.0
    events: int = 0
    sessions: int = 0


@dataclass
class TeamReport:
    since_day: str
    through_day: str
    total_usd: float
    sessions: int
    blocks: int
    by_dev: List[GroupSpend]
    by_project: List[GroupSpend]
    by_day: List[GroupSpend]
    top_sessions: List[Dict[str, object]]
    has_labels: bool

    def to_dict(self) -> Dict[str, object]:
        def groups(items: List[GroupSpend]) -> List[Dict[str, object]]:
            return [
                {
                    "name": g.name,
                    "cost_usd": round(g.cost_usd, 6),
                    "events": g.events,
                    "sessions": g.sessions,
                }
                for g in items
            ]

        return {
            "tool": "tokentab",
            "schema": 1,
            "kind": "guard_report",
            "window": {"start": self.since_day, "end": self.through_day},
            "totals": {
                "cost_usd": round(self.total_usd, 6),
                "sessions": self.sessions,
                "blocks": self.blocks,
                "labels_present": self.has_labels,
            },
            "by_dev": groups(self.by_dev),
            "by_project": groups(self.by_project),
            "by_day": groups(self.by_day),
            "top_sessions": self.top_sessions,
        }


def build_team_report(
    events: Iterable[Event],
    days: int = 7,
    now: Optional[datetime] = None,
) -> TeamReport:
    now = now or datetime.now().astimezone()
    today = now.astimezone().date()
    since = today - timedelta(days=max(1, days) - 1)
    since_key = since.isoformat()

    all_events = [e for e in events if e.day and e.day >= since_key]
    incremental = incremental_events(all_events)
    blocks = sum(1 for e in all_events if e.decision == "block")

    devs: Dict[str, GroupSpend] = {}
    projects: Dict[str, GroupSpend] = {}
    days_map: Dict[str, GroupSpend] = {}
    day_sessions: Dict[str, set] = defaultdict(set)
    sessions: Dict[str, Dict[str, object]] = {}
    has_labels = False

    for item in incremental:
        event = item.event
        if event.dev:
            has_labels = True
        for bucket, name in (
            (devs, item.dev),
            (projects, event.project or "(unknown)"),
            (days_map, item.day),
        ):
            entry = bucket.get(name)
            if entry is None:
                entry = GroupSpend(name=name)
                bucket[name] = entry
            entry.cost_usd += item.delta_usd
            entry.events += 1
        day_sessions[item.day].add(event.session_id)

        session = sessions.get(event.session_id)
        if session is None:
            session = {
                "session_id": event.session_id,
                "dev": event.dev or "(unlabeled)",
                "project": event.project or "(unknown)",
                "day": event.day,
                "cost_usd": 0.0,
                "blocks": 0,
            }
            sessions[event.session_id] = session
        session["cost_usd"] = float(session["cost_usd"]) + item.delta_usd
        if event.decision == "block":
            session["blocks"] = int(session["blocks"]) + 1
        if event.cost_usd >= float(session.get("max_cost") or 0):
            session["max_cost"] = event.cost_usd
            session["day"] = event.day

    session_ids = set(sessions)
    for bucket in (devs, projects):
        for entry in bucket.values():
            entry.sessions = sum(
                1
                for sid, s in sessions.items()
                if (s["dev"] if bucket is devs else s["project"]) == entry.name
            )
    for entry in days_map.values():
        entry.sessions = len(day_sessions.get(entry.name, set()))

    top = sorted(sessions.values(), key=lambda s: -float(s["cost_usd"]))[:10]
    for item in top:
        item["cost_usd"] = round(float(item["cost_usd"]), 6)
        item.pop("max_cost", None)

    return TeamReport(
        since_day=since_key,
        through_day=today.isoformat(),
        total_usd=sum(item.delta_usd for item in incremental),
        sessions=len(session_ids),
        blocks=blocks,
        by_dev=sorted(devs.values(), key=lambda g: -g.cost_usd),
        by_project=sorted(projects.values(), key=lambda g: -g.cost_usd),
        by_day=sorted(days_map.values(), key=lambda g: g.name),
        top_sessions=top,
        has_labels=has_labels,
    )


def render_team(report: TeamReport) -> str:
    def money(value: float) -> str:
        return f"${value:,.2f}"

    lines = [
        f"TokenTab guard ledger — {report.since_day} → {report.through_day}",
        "",
        f"total     {money(report.total_usd)} across {report.sessions} session(s)",
        f"blocks    {report.blocks}",
    ]
    if not report.has_labels and report.sessions:
        lines.append(
            "note      no dev labels in ledger; set \"dev\" in the guard config"
        )
    lines.append("")

    def section(title: str, groups: List[GroupSpend], limit: int = 10) -> None:
        if not groups:
            return
        lines.append(title)
        top = max((g.cost_usd for g in groups), default=0.0) or 1.0
        for group in groups[:limit]:
            filled = max(0, min(12, round(group.cost_usd / top * 12)))
            bar = "█" * filled + "░" * (12 - filled)
            lines.append(
                f"  {money(group.cost_usd):>12}  {bar}  {group.name}  "
                f"({group.sessions} session(s))"
            )
        lines.append("")

    section("By developer", report.by_dev)
    section("By project", report.by_project)
    section("By day", report.by_day)

    if report.top_sessions:
        lines.append("Top sessions")
        for session in report.top_sessions:
            lines.append(
                f"  {money(float(session['cost_usd'])):>12}  "
                f"{str(session['day']):<10}  {str(session['dev']):<12}  "
                f"{str(session['project'])[:38]}"
            )
        lines.append("")

    if not report.by_dev and not report.by_project:
        lines.append("No ledger rows in this window.")
    return "\n".join(lines).rstrip() + "\n"
