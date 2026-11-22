# SpendTab

Spend audit and budget guard for Claude Code. Reads your local session transcripts
and answers: **how much did I spend, on what, and where is money leaking?** Then it
can stop the next tool call when a session, project, or day crosses a budget.

No API keys. No uploads. Message bodies never leave your machine. The only outbound
request is an optional webhook you configure yourself.

MIT-licensed and free. This is an independent project: it is not affiliated with,
sponsored by, or endorsed by Anthropic. Claude and Claude Code are trademarks of
Anthropic. The maintainer is in a region where Anthropic services are unsupported, so
Claude Code-specific support may be limited; see `CONTRIBUTING.md`.

Three surfaces:

- `spendtab` — the report: totals, projects, models, cache efficiency, anomalies.
- `spendtab guard` / `spendtab hook pretooluse` — enforcement: a PreToolUse hook that
  exits 2 and blocks the tool call when a budget is exceeded.
- `spendtab guard report` — the ledger: spend by developer, project, and day, plus
  blocked calls, for invoices and team attribution.

```
SpendTab — Claude Code spend audit

sessions  4 total · 4 priced · 0 unpriced
window    2026-10-23 → 2026-11-22
total     $341.45
run-rate  $341.45/30d  (extrapolated)

By project
     $299.35  ██████████  /Users/demo/webapp  3 session(s)
      $42.10  █░░░░░░░░░  /Users/demo/data    1 session(s)

By model
     $184.20  claude-opus-5-5               129,320,000 in ·     18,000 out · cache 99%
     $138.50  claude-sonnet-5-5              86,100,000 in ·     15,000 out · cache 99%
      $18.75  claude-haiku-5-5                  480,000 in ·      4,200 out · cache 19%

Findings
  WARN  [low_cache_hit] Cache hit only 19% on a $18.75 session
        Most billed input is being re-sent cold. Keep the working set stable...
  INFO  [stale_1h_cache] 210,000 tokens written to 1h cache, only 90,000 read back
```

Sample output uses synthetic transcripts.

## Install

Requires Python 3.9+. No third-party dependencies.

```bash
# from this repo
pipx install .
# or
uv tool install .

# or run without installing
python3 -m spendtab
```

## Usage

```bash
spendtab                          # all sessions, all projects
spendtab --since 30d              # last 30 days
spendtab --since 2026-10-01       # from a date
spendtab --project myapp          # substring on project name or path
spendtab --json                   # stable machine-readable output
spendtab --csv                    # one row per session, for invoices/exports
spendtab --top 20                 # more sessions in the table
spendtab --fail-over 500          # exit 3 if spend exceeds $500 (CI/cron guard)
spendtab --no-color --no-cache

spendtab guard report             # ledger spend by dev/project/day (last 7 days)
spendtab guard report --days 30 --json
```

`--since` accepts `7d`, `36h`, `4w`, or `YYYY-MM-DD`.

## Guard: stop the next overspend

```bash
spendtab guard init
```

This writes `~/.config/spendtab/guard.json` and prints a hook snippet. The starter
file uses `"mode": "warn"` so installing it cannot block work on the first day;
switch to `"block"` after a week of observing the warnings. Add the snippet to
`.claude/settings.local.json` (this project) or `~/.claude/settings.json` (all
projects):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          { "type": "command", "command": "spendtab hook pretooluse" }
        ]
      }
    ]
  }
}
```

Verify against a live transcript without blocking anything:

```bash
spendtab guard check --transcript ~/.claude/projects/<project>/<session>.jsonl \
  --project "$PWD"
```

Config fields:

| Field | Default | Meaning |
|---|---|---|
| `session_budget_usd` | 5 | Per-session ceiling |
| `daily_budget_usd` | 25 | All projects, local calendar day |
| `project_daily_budget_usd` | 15 | Per-project ceiling, local day |
| `project_budgets` | `{}` | Path-prefix overrides, e.g. `{"~/work/client-a": 50}` |
| `mode` | `"block"` | `block` exits 2; `warn` allows and prints a warning |
| `dev` | `null` | Label written to the ledger for attribution |
| `dev_daily_budget_usd` | `null` | Optional daily ceiling for this `dev` label |
| `webhook_url` | `null` | Optional Slack-style `POST {"text": ...}` target; https, or http on loopback only |
| `webhook_on` | `"block"` | `block`, `always` (block+warn), or `never` |

Defaults apply when a field is omitted. `guard init` writes explicit starter values
(10/50/30/40) in `warn` mode.

How enforcement works:

- The hook receives `session_id`, `cwd`, `transcript_path`, and `tool_name` on
  stdin. It reads the last `cost-state` snapshot from the transcript, evaluates the
  three budgets, appends a ledger row, and exits 2 with the reason on stderr when a
  budget is crossed. Claude Code blocks that tool call.
- Daily and project totals come from `~/.local/state/spendtab/ledger.jsonl`, using
  the newest snapshot per session — repeated hook calls never double-count.
- Missing config, unreadable transcript, or unknown spend fails open: the call is
  allowed. A guard that bricks work is worse than a missed block.
- `spendtab hook pretooluse --dry-run` prints the decision and writes nothing.
- Webhooks fire once per state change (allow→warn→block), not on every blocked
  call, so a runaway session cannot flood the channel. Failure to deliver is
  ignored; the tool call decision is unaffected. The payload contains the budget
  numbers, project path, and dev label — never prompts, message text, or file
  content.
- Scanner and guard both use local time for daily budgets.

## Team ledger

```bash
spendtab guard report              # last 7 days
spendtab guard report --days 30
spendtab guard report --json
spendtab guard report --csv        # grouped rows: dev/project/day
```

Every hook run appends a ledger row (`~/.local/state/spendtab/ledger.jsonl`) with the
dev label, project, tool, cumulative session cost, and decision. The report converts
cumulative snapshots into per-event deltas, so a long session is not counted in full
every day. Rows older than 30 days are pruned when the ledger passes 8 MB.

The ledger is per-machine. It does not merge machines and does not talk to a server,
so "shared budget" means one machine or a shared state directory, not a fleet.

Hook limits that matter:

- A project can disable hooks (`disableAllHooks`, `allowManagedHooksOnly`) or simply
  not install the snippet. This is a spending guardrail, not a security control.
- PreToolUse does not fire for `@`-referenced files that are inlined without a tool
  call, so those reads are not blocked.
- Cost snapshots lag the work: the guard checks the latest recorded cost, so the
  final tool call of a turn can push a session past budget before the next check.
- The hook scans the last 8 MB of a transcript for the snapshot. If no snapshot is
  found, session cost reads as zero and only ledger-based budgets apply.

## Try the demo

```bash
python3 examples/make_demo.py --dir /tmp/spendtab-demo
spendtab --root /tmp/spendtab-demo/projects --no-cache
```

The demo writes five synthetic sessions with invented costs; it never reads real
transcripts.

## What it reads

`~/.claude/projects/**/*.jsonl` — the transcripts Claude Code already writes.
SpendTab only extracts:

- `cost-state` records: per-session cost, per-model token usage, 1h/5m cache
  split, durations, unknown-model flags
- `assistant` records: message counts for sessions that lack a cost snapshot

Message bodies, tool outputs, and file contents are never stored or printed.

## Findings

| Code | Severity | Trigger |
|---|---|---|
| `low_cache_hit` | warn | Priced session with ≥100k billed input tokens whose cache hit rate is below 50% |
| `stale_1h_cache` | info | ≥100k tokens written to 1h cache but fewer tokens read back |
| `model_low_cache_hit` | info | Model-wide cache hit rate below 30% on ≥1M billed input tokens |
| `cost_concentration` | info | One session is ≥40% of total spend (totals ≥ $100) |
| `unknown_model_cost` | warn | Sessions include models without known pricing; totals are a lower bound |
| `unpriced_sessions` | info | Sessions without a cost snapshot are excluded from totals |

Thresholds are deliberately conservative. A finding is a prompt to look, not a
proven dollar saving.

## Cache

First scan parses every transcript; subsequent scans reuse a local index at
`~/.cache/spendtab/index.json` (or `$XDG_CACHE_HOME/spendtab/index.json`) keyed by
file mtime, size, and a head/tail fingerprint so a rewrite that preserves timestamps
is still re-parsed. Deleting the file is safe. Disable with `--no-cache` or
`SPENDTAB_NO_CACHE=1`.

Measured on a 68-file / 206k-line transcript corpus: **14s first scan, 0.3s
cached**.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | report printed / call allowed |
| 1 | `spendtab guard check` found a blocked state |
| 2 | bad arguments, transcript root missing, or hook blocked the tool call |
| 3 | total spend exceeded `--fail-over` |

## Limitations

- Claude Code must write `cost-state` records. Sessions from older versions
  appear as unpriced and are excluded from totals.
- Costs come from the transcript's own pricing. SpendTab does not apply its own
  price table, so unknown models are flagged rather than guessed.
- Cost state is cumulative; the last snapshot in a transcript wins.
- A session id repeated across transcript files (copies, resumed transcripts) is
  counted once, keeping the snapshot with the most state.
- Only Claude Code transcripts are supported. Codex/Cursor logs are not read.
- Findings explain ratios; they do not estimate savings.

## Development

```bash
python3 -m unittest discover -s tests -t .
```

The suite uses synthetic transcripts only.

## Experimental: Gemini CLI spike

`prototype/gemini/` contains an isolated feasibility spike for Gemini CLI: AfterModel
usage ledger, list-price cost estimate, BeforeTool budget block, and a report. It is
not shipped in the wheel and has no support commitment. See
`prototype/gemini/README.md` for the hook setup and cost-model limits.

## Contributing

MIT-licensed contributions are welcome. Read `CONTRIBUTING.md` first: never commit
real transcripts or costs, keep the package dependency-free, and use synthetic
fixtures. Security reports go through GitHub's private advisory flow, not a public
issue.

## License

MIT. See `LICENSE`. Data handling and the threat model are documented in
`SECURITY.md`. SpendTab is not affiliated with or endorsed by Anthropic; use it only
where the services it reads are permitted to you.
