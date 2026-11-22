# Gemini CLI spike (experimental)

Not part of the SpendTab package or wheel. This is a feasibility spike for pricing
Gemini CLI usage and blocking a tool call against a budget.

## Why this can work

- Gemini CLI exposes `AfterModel` hook input with `llm_response.usageMetadata`
  (`promptTokenCount`, `candidatesTokenCount`, `cachedContentTokenCount`,
  `thoughtsTokenCount`, `toolUsePromptTokenCount`) plus `session_id`, `cwd`, and a
  timestamp. Verified in the Gemini CLI source at `hookTranslator.ts` and
  `hookEventHandler.ts`.
- `BeforeTool` hooks can block with exit code 2 (or `{"decision":"deny"}`).
- Gemini CLI telemetry can also emit `gemini_cli.api_response` with token counts, but
  it is opt-in; hooks do not require telemetry.

## Setup

```bash
# 1. Write a starter guard config (warn mode) and print the hook snippet.
python3 prototype/gemini/spike.py init --write-config > /tmp/spendtab-gemini-hooks.json

# 2. Merge the "hooks" object into .gemini/settings.json (project) or
#    ~/.gemini/settings.json (all projects). The snippet uses absolute paths.
```

The generated snippet registers:

- `AfterModel` → `spike.py after-model`: append one usage row per model response.
- `BeforeTool` → `spike.py before-tool`: exit 2 when a budget is exceeded.

Then run Gemini CLI normally:

```bash
gemini -p "say hello"
python3 prototype/gemini/spike.py report --days 7
```

## Synthetic test (no Gemini account needed)

The hook input shape is the one Gemini CLI builds:

```bash
cat > /tmp/after.json <<'JSON'
{
  "session_id": "demo",
  "cwd": "/tmp/demo",
  "timestamp": "2026-11-23T12:00:00+00:00",
  "hook_event_name": "AfterModel",
  "llm_request": {"model": "gemini-2.5-pro"},
  "llm_response": {"usageMetadata": {
    "promptTokenCount": 1000,
    "cachedContentTokenCount": 200,
    "candidatesTokenCount": 100,
    "thoughtsTokenCount": 20
  }}
}
JSON

python3 prototype/gemini/spike.py after-model < /tmp/after.json
python3 prototype/gemini/spike.py report --days 7
```

Expected: `$0.002225` for the sample. The ledger never contains prompts or responses.

Guard dry-run:

```bash
printf '%s' '{"session_id":"demo","cwd":"/tmp/demo"}' \
  | python3 prototype/gemini/spike.py before-tool --dry-run
```

## Cost model

`prices.json` is a **standard paid-tier list-price estimate** retrieved 2026-11-23,
not a bill. It handles:

- input, output (including thoughts), cached input
- long-context tiers for Pro models (>200k prompt)
- dated price changes (Gemini 3.8/3.6 Flash until 2026-12-31 and after)

It does **not** model cache storage, Batch/Flex/Priority, audio, image, or tool
surcharges. Free-tier OAuth usage is $0 in reality; this table still shows a list-price
estimate. Before trusting numbers, compare against `/stats` and Google Cloud/API
billing for one real session.

## Limits

- Hooks are still a newer Gemini CLI surface; field names can change. The spike fails
  open: a malformed payload or broken config allows the tool call.
- `AfterModel` may fire once per model request, including intermediate tool steps; the
  ledger sums all of them. A duplicated hook registration would double-count. Run one
  hook per event.
- Session budget is the sum of all ledger rows for that session. Project/day budgets
  use the event's local `day`.
- No cross-machine aggregation, no GCP billing reconciliation.
- This spike is not in the SpendTab wheel and has no support commitment.

## Success criteria before promoting it

1. One real Gemini CLI session: token totals match `/stats` exactly, and cost is within
   1% of the official billing export for the same period.
2. `BeforeTool` actually blocks at a $0.01 session budget.
3. No prompt or response text appears in the ledger.
4. A second user (paid API/Vertex) can install it from the README without help.
