# Changelog

All notable changes are recorded here. The project follows semantic versioning for
the CLI and the JSON output schema.

## 0.2.0 — 2026-11-23

### Added

- `tokentab guard report` — ledger spend by developer, project, and day, with
  `--json` and grouped `--csv`.
- `tokentab --csv` — one row per session, including models, cost, cache hit rate,
  token counts, and API duration.
- Optional webhook on the first allow→warn→block transition. Only `https://` or
  loopback URLs are accepted; payloads contain budget numbers, project path, and dev
  label, never prompts or code.
- `dev_daily_budget_usd` and per-project path overrides.
- Ledger pruning (30 days) when the file passes 8 MB.
- Synthetic demo generator: `examples/make_demo.py`.

### Changed

- Budgets charge the day's spend delta, so a session resumed from yesterday is not
  recharged against today's project/day/dev budgets. Session budgets remain
  cumulative.
- `guard init` writes a starter in `warn` mode so the first install cannot block work.
- Cache keys include a head/tail fingerprint, and files appended during a scan are not
  cached.
- Duplicate session ids across transcripts are counted once.

### Fixed

- Crash on malformed `cost-state` fields, non-finite costs, and wrong-shape cache
  entries.
- Cross-midnight double counting of project/day/dev budgets.
- Invalid webhook ports escaping config load.
- Non-string `dev` labels breaking report grouping.
- Mixed timezone offsets ordering ledger deltas incorrectly.
- `--json` could emit `NaN`; totals now stay finite.

## 0.1.0 — 2026-11-23

- Initial scanner: totals by project/model/day/session, cache hit rate, findings,
  JSON output, `--fail-over`, local mtime cache.
- PreToolUse guard: session/project/day budgets, warn/block modes, ledger, dry-run,
  fail-open behavior.
