# Contributing

TokenTab is MIT-licensed. Bug reports, tests, docs, and focused pull requests are
welcome.

## Ground rules

- Never commit real Claude Code transcripts, project paths, tokens, or costs. Use
  synthetic fixtures or `examples/make_demo.py`.
- Keep the package dependency-free. The CLI must run on a stock Python 3.9+.
- The scanner is read-only with respect to `~/.claude`; the guard writes only its own
  cache, config, ledger, and optional webhook.
- The guard is a guardrail, not a security boundary. Do not market it as one.

## Development

```bash
python3 -m unittest discover -s tests -t . -v
python3 examples/make_demo.py --dir /tmp/tokentab-demo --force
python3 -m tokentab --root /tmp/tokentab-demo/projects --no-cache
python3 -m pip wheel --no-deps -w /tmp/dist .
```

Test on Python 3.9 if you change parsing or typing. CI runs 3.9, 3.11–3.14.

## Pull requests

- One concern per PR, with a test that fails before the change.
- For parsing changes, add a fixture covering the transcript shape and a malformed
  variant.
- For guard changes, cover allow/warn/block, cross-midnight, and malformed config.
- Update `CHANGELOG.md` for user-visible behavior.

## Reporting bugs

Use the bug report template. Include the TokenTab version, Python version, OS, the
command, and a synthetic reproduction. Redact prompts, paths, and costs.

Security issues: use GitHub's private security advisory, not a public issue. See
`SECURITY.md`.

## Maintainer scope

The maintainer cannot provide Claude Code-specific support from a region where
Anthropic services are unsupported. Issues that require reproducing against Claude
Code may remain unanswered. The code and tests are public so others can carry it.
