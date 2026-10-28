# Jira MCP Server (read-only)

A local [MCP](https://modelcontextprotocol.io) server that lets Claude Code pull Jira
ticket context (issue details, comment threads, the reference graph around a ticket,
and JQL search results) rendered as compact Markdown. Attachments can be fetched
too: images (e.g. the screenshot on a UI bug ticket) for Claude to analyze visually,
and text files (logs, stack traces, JSON/HAR, CSV, source code, ...) as plain text.

## What it deliberately cannot do

This server is **strictly read-only**. It exposes no tool that creates, updates,
transitions, deletes, or comments on anything. Enforcement is layered:

1. **In code:** every HTTP request funnels through a single helper that only permits
   `GET`, with one allowlisted exception: `POST /rest/api/3/search/jql`, a read
   operation that Atlassian requires be sent as POST. Any other method raises
   `ReadOnlyViolationError`, so a future edit that adds a write call fails loudly.
2. **At the credential level:** create the API token with only read scopes (below),
   so even a bug could not write.

## Tools

| Tool | Purpose |
|---|---|
| `get_issue(issue_key, include_comments=True)` | Full ticket detail including all non-empty custom fields (acceptance criteria, story points, ...) with their display names, plus (by default) the comment thread |
| `get_comments(issue_key, limit=100, newest_first=False)` | Just the discussion, with author/timestamp/edited/visibility |
| `get_issue_context(issue_key)` | Parent, subtasks, linked issues (with link direction), and epic children, each as key + type + status + summary |
| `search_issues(jql, limit=25)` | Compact JQL search results |
| `get_attachment(attachment_id)` | Downloads an attachment (listed by `get_issue`). Images (png/jpeg/gif/webp) come back as vision input, so Claude can look at screenshots; text files (detected by mime type, or by extension when Jira reports `application/octet-stream`) come back as text. Files of unknown type and extension (e.g. `Dockerfile`, `app.log.1`) are downloaded and served as text if their content has no NUL bytes and is valid UTF-8 (or UTF-16 with BOM). Text over 20,000 characters is saved in full (as UTF-8) under `<tmp>/jira-mcp/attachments/`, and the tool returns that path plus the first and last 50 lines, so Claude can grep or page through the file instead of loading it all. Max 5 MB; videos, PDFs, archives and other binary files are rejected |
| `whoami()` | Which account the token resolves to; the first stop for auth debugging |

## Setup

### 1. Create an Atlassian API token

1. Go to <https://id.atlassian.com/manage-profile/security/api-tokens>.
2. Choose **Create API token with scopes** (Atlassian is deprecating unscoped tokens).
3. Select the **Jira** app and pick **only** these scopes:
   - `read:jira-work`
   - `read:jira-user`
4. Copy the token immediately; it is shown only once.

An older *unscoped* token also works; the server handles both automatically (see below).

### 2. Configure `.env`

```bash
cp .env.example .env   # then edit
```

Required keys (this is the whole configuration surface):

| Key | Value |
|---|---|
| `ATLASSIAN_EMAIL` | The email of your Atlassian account |
| `ATLASSIAN_API_TOKEN` | The token from step 1 |
| `ATLASSIAN_SITE_URL` | e.g. `https://your-company.atlassian.net` |

`.env` is gitignored; never commit it. Real environment variables take precedence
over the file. The file is located relative to the project directory (not the working
directory), so the server finds it no matter where it is launched from.

### 3. Install dependencies

With [uv](https://docs.astral.sh/uv/) (preferred, since this repo has a `uv.lock`):

```bash
uv sync
```

Or with plain pip into a venv:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip
```

### 4. Verify with `--check`

```bash
.venv/bin/python -m jira_mcp --check            # connectivity + auth only
.venv/bin/python -m jira_mcp --check PROJ-123   # also fetch a ticket in full
```

This prints whether `.env` was found, which base URL was selected (and whether the
cloud-ID fallback was needed), the authenticated account, and, when a key is given,
the ticket exactly as Claude would see it.

## Scoped vs. unscoped tokens: the base URL problem

- An **unscoped** token works against your site URL, `https://<site>.atlassian.net`.
- A **scoped** token against that same URL fails *silently*, returning
  anonymous-looking responses. It must call
  `https://api.atlassian.com/ex/jira/{cloudId}` instead.

You do not need to know which kind you hold. At startup the server probes the site URL
with `GET /rest/api/3/myself`; if that does not return a real account, it fetches your
cloud ID from `{site}/_edge/tenant_info` and retries against `api.atlassian.com`. The
winner is cached for the process lifetime and logged to stderr.

If detection ever fails: `_edge/tenant_info` is not part of Atlassian's formally
supported REST API (though Atlassian's own support docs point at it), so it could
change. In that case set `ATLASSIAN_CLOUD_ID` in `.env` to skip detection; the error
message will tell you when this applies. You almost never need it.

## PyCharm setup

1. **Interpreter:** Settings → Project → Python Interpreter → Add Interpreter →
   *Existing* → select `.venv/bin/python` in the project directory. (If you ran
   `uv sync`, the venv already exists with everything installed.)
2. **Run configuration for debugging:** Run → Edit Configurations → **+** → Python:
   - **Run:** module `jira_mcp` (choose "module" instead of "script path")
   - **Parameters:** `--check PROJ-123`
   - **Working directory:** the project root (anything works, but this is tidy)

   Now you can set breakpoints anywhere (e.g. in `client.py`) and debug real requests.
   Errors inside a running MCP server are otherwise invisible.

## Connect to Claude Code

Use the venv's Python **by absolute path**; a bare `python` won't resolve to the venv
when Claude Code spawns the server.

macOS/Linux:

```bash
claude mcp add jira -- /path/to/PythonProject/.venv/bin/python -m jira_mcp
```

Windows:

```bash
claude mcp add jira -- C:\path\to\PythonProject\.venv\Scripts\python.exe -m jira_mcp
```

Notes:

- Everything after `--` is the command Claude runs; everything before it is Claude's
  own options.
- Default scope is `local` (just you, just this project, stored in `~/.claude.json`).
  Add `--scope project` to share via a checked-in `.mcp.json`, or `--scope user` to use
  it across all your projects.

### Verify it's connected

Inside a Claude Code session:

- Run `/mcp`; the `jira` server should be listed as connected, with six tools.
- Or just ask: *"use whoami to check the jira connection"*.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| **401 Unauthorized** | Wrong email or token, or the token was revoked/expired. Recreate the token and update `.env`. Run `--check` to confirm. |
| **403 Forbidden** | Scoped token missing `read:jira-work` / `read:jira-user`, or your account lacks site access. Recreate the token with both read scopes. |
| **404 Not Found** | The issue doesn't exist, **or your account lacks permission to see it**. Jira reports issues you cannot view as 404, and a token never grants more access than the human it belongs to. Verify you can open the ticket in a browser while logged in as that account. |
| **Empty tool list in Claude** | The server crashed at startup. Run the exact command from `claude mcp add` yourself in a terminal; startup errors print to stderr. Usual causes: wrong Python path, or missing `.env` keys. |
| **Server won't start** | Run `--check`. If it reports missing config, fix `.env`. If imports fail, re-run `uv sync` (or reinstall `requirements.txt`) and confirm the venv Python is ≥ 3.11. |
| **Detection failed / anonymous responses** | Startup logs (stderr) say which base URL was probed and why it was rejected. If `_edge/tenant_info` is unreachable, set `ATLASSIAN_CLOUD_ID` in `.env`. |

## Notes for developers new to Python

- **The venv** (`.venv/`) is a project-local copy of Python plus this project's
  packages: the equivalent of `node_modules`, except the interpreter itself lives
  inside it too. That's why Claude Code must be given `.venv/bin/python` by absolute
  path: there's no global install to fall back on.
- **`asyncio.run(...)`** is needed because async functions in Python don't run just by
  calling them; calling one returns a *coroutine object*, and something has to drive
  it. There's no ambient event loop like in Node; `asyncio.run()` creates a loop, runs
  one coroutine to completion, and tears the loop down. The MCP server does this
  internally via `mcp.run()`; the `--check` mode does it explicitly.
- **The decorators** (`@mcp.tool`) are functions that receive the function defined
  below them and register/wrap it, like a middleware factory applied at definition
  time. FastMCP's decorator reads the function's name, type hints, and docstring to
  generate the MCP tool schema that Claude sees; the docstring *is* the tool's API
  documentation.
- **`python -m jira_mcp`** runs the package's `__main__.py`, the closest thing Python
  has to an npm `bin` entry. It works from any directory because `uv sync` installed
  the project into the venv.
