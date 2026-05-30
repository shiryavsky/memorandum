# Contributing to Memorandum

Thanks for your interest. Memorandum is a small, opinionated personal tool —
contributions are welcome, but the design philosophy is biased toward
keeping the surface area small and the code obvious. Read this once before
opening a PR; it'll save us both round-trips.

## Quick start (dev setup)

```bash
git clone <fork-url> memorandum
cd memorandum
./setup.sh                            # creates .venv, installs requirements + requirements-dev.txt
cp config.example.yaml config.yaml   # fill in your own sources
.venv/bin/pytest tests/              # 600+ tests; should be all green
```

CI runs Python 3.11. The codebase uses 3.11+ features (`X | None` syntax,
`list[...]` builtins); older interpreters won't work.

## Running the things you'll touch

```bash
.venv/bin/python -m cli health             # ingest status
.venv/bin/python -m cli dashboard          # live TUI
.venv/bin/python -m cli dashboard --mock   # no DB / config needed — for screenshots & UI dev
.venv/bin/python -m cli prune --dry-run    # preview retention deletes
./run_ingest.sh --hours 24                 # one-shot ingest
```

## Where things live

- `connectors/` — one module per source (Mattermost / Telegram / Pachca / Email).
  Read [`connectors/CONTRIBUTING.md`](connectors/CONTRIBUTING.md) before adding a fifth.
- `pipeline/` — the ingest engine + retention + dashboard data layer + alias resolver.
  None of these modules import from `mcp_server/` or `connectors/`; that's deliberate.
- `mcp_server/server.py` — the MCP tool surface. Each tool is one `_<name>(args)` handler.
- `storage/db.py` — all SQL lives here. Don't open `pipeline/dashboard.py` query
  files; the data layer calls instance methods on `Database`.
- `cli/` — user-facing CLI verbs. `cli/__main__.py` is the argparse dispatcher;
  one module per verb.
- `tests/` — pytest. Conftest mocks `storage.vector_store` globally so BGE-M3
  never loads in CI.
- `AGENTS.md` — architectural reference. Read first if you're touching anything
  non-trivial.

## Style and conventions

- **Tests are required.** Every behavior change ships with at least one test.
  Bug fixes ship with a regression test that fails on the parent commit and
  passes on yours.
- **Strict flake8 must be clean** (`flake8 . --select=E9,F63,F7,F82`). The
  soft pass (line length, complexity) is `--exit-zero` and only a guideline.
  Line length cap is 127.
- **Comments only when WHY isn't obvious.** No "what this does" narration of
  self-evident code. Multi-line comment blocks are rare; one short line is
  usually right.
- **Don't add features beyond what the task / issue asks for.** Memorandum
  deliberately keeps the surface small. Cross-cutting refactors are a
  separate PR.
- **No mocking the DB.** Tests run against an in-memory SQLite (via the real
  `Database` class). Vector store is mocked because BGE-M3 is too heavy.
- **Connectors mock the HTTP layer.** Use `responses` for `requests`-based
  connectors, `MagicMock` for `imap_tools`.

## Commit messages

Short imperative summary on the first line. Body explains *why* (the *what*
is in the diff). Examples that fit the project's voice:

```
Fix retention sweep counting freshly-written files as orphans

The grace period parameter was off-by-one when min_age_seconds=0 —
the cutoff_mtime comparison used `>` instead of `>=`, so files written
in the same second as the sweep were spared. Tightened the comparison
and added a regression test.
```

```
Add Slack connector

Implements the connector contract documented in connectors/CONTRIBUTING.md.
Uses the Slack Web API (chat.history) with cursor pagination; per-channel
incremental sync via `last_update_at` like Mattermost.
```

No need to prefix with "feat:" / "fix:" — the convention here is plain
English. If you reference an issue, do it in the body (`Fixes #42`), not
the subject.

## What gets reviewed

When you open a PR, the reviewer will look at:

1. **Does it do what it claims?** Run locally, exercise the new path.
2. **Is the change scoped?** No drive-by reformatting, no unrelated refactors.
3. **Are the tests meaningful?** Asserting on shape and behavior, not on
   internal implementation details.
4. **Did you update the relevant doc?** README for user-visible changes,
   `AGENTS.md` for architectural ones, `connectors/CONTRIBUTING.md` if you
   added/changed a connector.
5. **No personal data in test fixtures.** Use `Jane Smith` / `Bob Wilson` /
   `john.doe` / `acme.com` — the convention already in the codebase. The
   project's threat model assumes the repo is public-facing.

## Reporting bugs / requesting features

Use the issue templates. For Q&A or "is this the right way to do X",
use Discussions (linked from the issue picker). Security issues go through
GitHub Security Advisories — see [SECURITY.md](SECURITY.md).

## Code of conduct

Be kind. The full [Code of Conduct](CODE_OF_CONDUCT.md) applies to all
project spaces (issues, PRs, discussions, commits).
