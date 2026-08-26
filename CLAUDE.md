# CLAUDE.md

## Project Goal

Build a local MCP (Model Context Protocol) server that pulls Jira ticket context into Claude Code. The project started as a mostly empty PyCharm Python project.

## Language & Tooling

- Python only. Dependencies are managed with `uv` (`pyproject.toml` + `uv.lock`).
- Jira credentials live in `.env` (never committed, never read by Claude). Use `.env.example` as the template for required variables.

## Code Style

- **Typing is required.** All functions and methods must have type hints for parameters and return values.
- **Comments:** only write them when the code is not self-explanatory. When they do appear, keep them short and concise. Prefer clear naming over comments.

## Testing

- We want test coverage for the code in this repo. New functionality should come with tests; use `pytest`.

## Verification Pipeline

Formatting and linting run automatically via a hook (`ruff format` + `ruff check --fix`) after every file edit.

After completing a change — and always before committing — run and pass:

1. `uv run mypy .` — type checking
2. `uv run pytest` — test suite

Do not consider a change done while either fails.

## Commits

- Use Conventional Commits, e.g. `feat:`, `fix:`, `chore:`, `refactor:`, `test:`, `docs:`.
