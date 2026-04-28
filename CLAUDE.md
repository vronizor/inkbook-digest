# CLAUDE.md

Conventions for working on this repo.

## Code style

- **Minimal comments.** Code should be self-explanatory through naming. Comment only non-obvious *why*, never *what*.
- **No docstrings on simple functions.** Add them only for module-level entry points or where the contract is non-trivial.
- **Type hints everywhere.** Use built-in generics (`list[str]`, not `List[str]`).
- **No `from x import *`.** Explicit imports.
- **f-strings, not %-formatting or .format().**
- **`pathlib.Path`, not `os.path`.**
- **`datetime.now(timezone.utc)`, never naive datetimes** in stored/transmitted data. Local time only at the presentation layer.

## Dependencies

- `uv` for everything. `uv add <pkg>` to add, `uv sync` to install.
- Pin Python to 3.12 in `pyproject.toml`.
- Keep deps to: `httpx`, `ebooklib`, `apscheduler`, `pillow`. Stdlib for the rest.

## Error handling philosophy

- Fail fast on config errors at startup. No silent fallbacks for missing env vars.
- For per-article failures during a digest run: log + continue + report in the alert email. One bad article must not block the digest.
- For Reader API errors: distinguish 4xx (likely permanent — log and skip article) from 5xx/network (transient — let the next day's run retry).
- No bare `except:`. Always catch specific exceptions or `Exception` with explicit logging.

## What not to do

- **No premature abstraction.** This is a 250-line personal tool. Avoid base classes, plugin architectures, dependency injection frameworks.
- **No retry loops with exponential backoff inside a single run.** If something fails mid-run, fail and let the next scheduled run handle it. Daily cadence makes most retries pointless anyway.
- **No alembic / SQL migrations.** SQLite schema is set up via `CREATE TABLE IF NOT EXISTS` at startup. Schema changes during dev = delete the SQLite file.
- **No `pytest` plugin sprawl.** Stdlib `unittest` is fine; `pytest` core only if it makes things meaningfully cleaner.
- **No metrics, no Prometheus, no Grafana.** Stdout logs and email alerts are the observability stack.
- **No async unless `httpx` makes it materially simpler.** Sync code is fine; we're not throughput-bound.
- **Don't add features not in SPEC.md.** Ask first.

## Verification before declaring done

- The Reader API has two unverified assumptions in SPEC.md (server-side tag filter, PATCH for tag mutation). **Verify these with a real API call before writing code that depends on them.** Use a throwaway script with the user's token. If verification fails, implement the documented fallback.
- Run `docker compose up --build` locally before declaring deployment ready.
- Test the `--once` flag works inside the running container.

## Communication style with the user

- Vincent is a data analyst with a PhD in economics. Comfortable with Python, SQL, Docker, statistics. Skip explanations of basics.
- He prefers no preamble, direct technical answers, and pushback when warranted. Don't be a yes-machine.
- When in doubt about a design choice, ask. Don't invent.
