# Open core

SpendTab is open source under MIT. The CLI, the budget guard, the local ledger, and
the JSON/CSV output are the core, and they stay free and fully functional without
an account.

## What stays in the open core (MIT)

- Transcript parsing, cost calculation, cache-efficiency findings, anomalies.
- `spendtab guard` and the PreToolUse hook, including local budgets and blocking.
- `spendtab guard report` for one machine, JSON and CSV.
- Optional single webhook per machine.
- No telemetry, no account, no uploads.

## What may become a paid hosted layer

These are ideas, not promises, and none of them exist today:

- Team dashboard: many developers and machines syncing ledgers to one place.
- Cross-machine budgets, alert routing (Slack, email, PagerDuty), retention beyond
  local limits.
- SSO, audit export, invoice-grade reporting.

The boundary rule: anything that requires a server, identity, or multi-tenant
storage belongs to the hosted layer. Anything that runs on one machine stays in the
MIT core.

## What this means in practice

- No feature will be removed from the core to push a paid plan.
- The hosted layer, if it is ever built, will read the same ledger format the CLI
  already writes, so self-hosting remains possible.
- Issues about the core are handled in this repository; hosted support terms would be
  separate.
