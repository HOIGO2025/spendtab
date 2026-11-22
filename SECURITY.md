# Security and privacy

TokenTab runs locally and has no telemetry.

## What it reads

- `~/.claude/projects/**/*.jsonl` for `cost-state` and message-count metadata.
- Message bodies, tool outputs, prompts, and file contents are never stored in the
  cache or the ledger, and never printed.

## What it writes

| Path | Contents |
|---|---|
| `~/.cache/tokentab/index.json` | Per-file derived metadata: project path, session id, model names, token counts, costs, durations. Keyed by mtime, size, and a head/tail fingerprint. |
| `~/.config/tokentab/guard.json` | Budgets, dev label, optional webhook URL. Created by `guard init`. |
| `~/.local/state/tokentab/ledger.jsonl` | One row per hook call: time, session id, project path, dev label, tool name, cumulative cost, delta, allow/warn/block decision. Pruned to 30 days when it passes 8 MB. |

Set `XDG_CACHE_HOME` / `TOKENTAB_STATE_DIR` / `TOKENTAB_GUARD_CONFIG` to move these.

## Network

There are no outbound requests unless you set `webhook_url` in the guard config.
When set, a POST is sent once per allow→warn→block transition with:

```json
{"text": "<budget message with numbers and project path>", "dev": "<label>"}
```

Only `https://` URLs are accepted, plus `http://` on loopback for local testing.
Credentials in the URL are rejected. Delivery failure is ignored and never changes
the tool-call decision or blocks work for more than the 1.5s timeout.

## Hook model

The guard is a spending guardrail, not a sandbox. A project can disable hooks
(`disableAllHooks`, `allowManagedHooksOnly`) or omit the settings snippet.
PreToolUse does not fire for `@`-inlined files, and cost snapshots lag the work.
Do not rely on TokenTab to contain a hostile process or to enforce policy against
a user who controls their own settings.

## Reporting a vulnerability

Use GitHub's private security advisory for this repository. Do not open a public
issue with exploit details. Include a minimal reproduction and the TokenTab version.
