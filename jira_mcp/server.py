"""The MCP server: tool definitions, --check CLI mode, entry point.

stdio discipline: stdout carries only MCP protocol traffic, so nothing here
may print() in server mode. All diagnostics go to stderr via logging.
(--check mode runs outside MCP entirely and prints freely.)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any, NoReturn

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.utilities.types import Image

from .client import MYSELF_PATH, JiraClient, JiraError
from .config import ENV_FILE, ConfigError, Settings, load_settings
from .render import (
    format_size,
    normalize_issue_key,
    render_comments,
    render_context,
    render_issue,
    render_myself,
    render_search_results,
)

logger = logging.getLogger(__name__)

mcp: FastMCP = FastMCP(
    name="Jira (read-only)",
    instructions=(
        "Read-only access to Jira Cloud issues.\n"
        "\n"
        "When to use: whenever a Jira issue key appears in the conversation. "
        "A key is an uppercase project prefix, a hyphen, and a number (e.g. "
        "ABC-123, PLAT-4567); keys also show up inside branch names, commit "
        "messages, and Jira URLs. On seeing one, fetch the ticket instead of "
        "guessing what it contains.\n"
        "\n"
        "Workflow: start with get_issue for the full ticket, then "
        "get_issue_context to find parents, subtasks, and linked issues worth "
        "reading. Use search_issues (JQL) when no key is given. When a ticket "
        "lists image attachments relevant to the task (e.g. a screenshot on a "
        "UI bug), fetch them with get_attachment to see them. Call whoami "
        "only to debug authentication.\n"
        "\n"
        "Constraints: strictly read-only; issues cannot be created, edited, "
        "or transitioned."
    ),
)

# One client per process, created on first use. Lazy so that importing this
# module (e.g. in tests) needs no credentials.
_client: JiraClient | None = None


def _get_client() -> JiraClient:
    global _client
    if _client is None:
        _client = JiraClient(load_settings())
    return _client


ISSUE_FIELDS = (
    "summary,status,issuetype,priority,resolution,assignee,reporter,"
    "labels,components,fixVersions,created,updated,description,attachment"
)
CONTEXT_FIELDS = "summary,status,issuetype,parent,subtasks,issuelinks"
SEARCH_FIELDS = ["summary", "status", "issuetype", "priority", "assignee", "updated"]

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024

# The only mime types Claude can analyse visually, mapped to the format name
# FastMCP's Image helper expects. Everything else (videos, PDFs, archives,
# logs) is rejected before download.
IMAGE_FORMATS = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
}


# -- shared implementations (used by both MCP tools and --check) ---------------


async def _fetch_comments(
    client: JiraClient, key: str, limit: int, newest_first: bool
) -> list[dict[str, Any]]:
    data = await client.get_json(
        f"/rest/api/3/issue/{key}/comment",
        params={
            "maxResults": max(1, min(limit, 100)),
            "orderBy": "-created" if newest_first else "+created",
        },
    )
    comments = data.get("comments") or []
    return list(comments)


async def _get_issue_impl(
    client: JiraClient, issue_key: str, include_comments: bool
) -> str:
    key = normalize_issue_key(issue_key)
    issue = await client.get_json(
        f"/rest/api/3/issue/{key}", params={"fields": ISSUE_FIELDS}
    )
    comments = (
        await _fetch_comments(client, key, 100, False) if include_comments else None
    )
    return render_issue(issue, comments)


async def _get_issue_context_impl(client: JiraClient, issue_key: str) -> str:
    key = normalize_issue_key(issue_key)
    issue = await client.get_json(
        f"/rest/api/3/issue/{key}", params={"fields": CONTEXT_FIELDS}
    )
    fields = issue.get("fields") or {}
    issue_type = fields.get("issuetype") or {}

    # Epics don't list their children on the issue itself; one extra search
    # resolves them. hierarchyLevel 1 == epic-level in Jira's type hierarchy.
    epic_children: list[dict[str, Any]] | None = None
    if (
        issue_type.get("hierarchyLevel") == 1
        or str(issue_type.get("name", "")).lower() == "epic"
    ):
        result = await client.search_jql(
            {
                "jql": f'parent = "{key}" ORDER BY created ASC',
                "maxResults": 50,
                "fields": ["summary", "status", "issuetype"],
            }
        )
        epic_children = list(result.get("issues") or [])

    return render_context(issue, epic_children)


async def _search_issues_impl(client: JiraClient, jql: str, limit: int) -> str:
    result = await client.search_jql(
        {"jql": jql, "maxResults": max(1, min(limit, 100)), "fields": SEARCH_FIELDS}
    )
    issues = list(result.get("issues") or [])
    return render_search_results(
        jql, issues, has_more=bool(result.get("nextPageToken"))
    )


async def _reject_issue_key_as_attachment_id(client: JiraClient, key: str) -> NoReturn:
    """Turn the issue-key-instead-of-ID mistake into a one-step recovery:
    the error itself lists the valid attachment IDs of that issue."""
    issue = await client.get_json(
        f"/rest/api/3/issue/{key}", params={"fields": "attachment"}
    )
    attachments = (issue.get("fields") or {}).get("attachment") or []
    if not attachments:
        raise ValueError(
            f"'{key}' is an issue key, not an attachment ID, and {key} has "
            "no attachments."
        )
    listing = "; ".join(
        f"{a.get('filename', '?')} = id {a.get('id', '?')}" for a in attachments
    )
    raise ValueError(
        f"'{key}' is an issue key, not an attachment ID. Attachments on {key}: "
        f"{listing}. Call get_attachment again with one of these numeric IDs."
    )


async def _get_attachment_impl(client: JiraClient, attachment_id: str | int) -> Image:
    att_id = str(attachment_id).strip()
    if not att_id.isdigit():
        try:
            key = normalize_issue_key(att_id)
        except ValueError:
            raise ValueError(
                f"'{attachment_id}' is not a valid attachment ID; expected the "
                "numeric ID shown in the Attachments section of get_issue "
                "(e.g. '10023')."
            ) from None
        await _reject_issue_key_as_attachment_id(client, key)

    # Metadata first: it carries mime type and size, so unsupported or
    # oversized files are rejected without downloading a single byte.
    meta = await client.get_json(f"/rest/api/3/attachment/{att_id}")
    filename = str(meta.get("filename") or att_id)
    mime_type = str(meta.get("mimeType") or "").lower()
    size = int(meta.get("size") or 0)

    image_format = IMAGE_FORMATS.get(mime_type)
    if image_format is None:
        supported = ", ".join(sorted(set(IMAGE_FORMATS)))
        raise ValueError(
            f"Attachment '{filename}' has type '{mime_type or 'unknown'}', which "
            f"cannot be analysed visually. Only images are supported: {supported}. "
            "Videos and other file types cannot be fetched."
        )
    if size > MAX_ATTACHMENT_BYTES:
        raise ValueError(
            f"Attachment '{filename}' is {format_size(size)}, above the "
            f"{format_size(MAX_ATTACHMENT_BYTES)} limit for inline analysis."
        )

    data, _ = await client.get_bytes(f"/rest/api/3/attachment/content/{att_id}")
    # Metadata can be stale or wrong; never trust it over the actual payload.
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError(
            f"Attachment '{filename}' downloaded as {format_size(len(data))}, above "
            f"the {format_size(MAX_ATTACHMENT_BYTES)} limit for inline analysis."
        )
    if not data:
        raise ValueError(f"Attachment '{filename}' has no content.")
    return Image(data=data, format=image_format)


async def _whoami_impl(client: JiraClient) -> str:
    account = await client.get_json(MYSELF_PATH)
    return render_myself(account, client.base_url or "?", client.used_cloud_id_fallback)


# -- MCP tools ------------------------------------------------------------------
# Docstrings are the API the model sees; they describe behaviour, not code.


@mcp.tool
async def get_issue(issue_key: str, include_comments: bool = True) -> str:
    """Get full detail for one Jira issue as Markdown.

    Returns summary, status, type, priority, resolution, assignee, reporter,
    labels, components, fix versions, created/updated timestamps, the full
    description, the attachment list (fetch images via get_attachment), and
    (by default) the complete comment thread.

    Args:
        issue_key: Jira issue key like "PROJ-123" (case and surrounding
            whitespace are tolerated).
        include_comments: Set False to omit the comment thread when only the
            ticket body is needed — the response is much shorter.

    Prefer get_issue_context when the goal is discovering *related* tickets
    rather than reading this one.
    """
    try:
        return await _get_issue_impl(_get_client(), issue_key, include_comments)
    except (JiraError, ConfigError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
async def get_comments(
    issue_key: str, limit: int = 100, newest_first: bool = False
) -> str:
    """Get only the comment thread of a Jira issue, as Markdown.

    Each comment includes author, timestamp, whether it was edited, and any
    visibility restriction. Use this instead of get_issue when the ticket
    body is already known and only the discussion is needed.

    Args:
        issue_key: Jira issue key like "PROJ-123".
        limit: Maximum number of comments to return (1-100).
        newest_first: Set True to get the most recent comments first —
            useful for long threads where only the latest state matters.
    """
    try:
        client = _get_client()
        key = normalize_issue_key(issue_key)
        comments = await _fetch_comments(client, key, limit, newest_first)
        return render_comments(comments)
    except (JiraError, ConfigError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
async def get_issue_context(issue_key: str) -> str:
    """Map the reference graph around a Jira issue: parent, subtasks, links.

    Returns the parent (e.g. epic), all subtasks, and all linked issues —
    each resolved to key + type + status + summary so the next issue to read
    can be chosen without further calls. Link relationships are given in
    human terms with correct direction ("blocks" vs "is blocked by"). For
    epics, also lists the issues inside the epic.

    Args:
        issue_key: Jira issue key like "PROJ-123".

    Use this first when exploring an unfamiliar ticket; then get_issue on
    the keys that look relevant.
    """
    try:
        return await _get_issue_context_impl(_get_client(), issue_key)
    except (JiraError, ConfigError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
async def search_issues(jql: str, limit: int = 25) -> str:
    """Search Jira with a JQL query; returns a compact Markdown list.

    Each result line has key, type, status, summary, assignee and last
    update. Example JQL: 'project = PROJ AND status != Done ORDER BY
    updated DESC'. Results are capped at `limit` (1-100); the output notes
    when more matches exist.

    Args:
        jql: A JQL query string (sent via POST, so any length/complexity works).
        limit: Maximum number of issues to return.
    """
    try:
        return await _search_issues_impl(_get_client(), jql, limit)
    except (JiraError, ConfigError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
async def get_attachment(attachment_id: str | int) -> Image:
    """Download an image attached to a Jira issue, for visual analysis.

    Returns the image itself so it can be looked at directly. Use it when a
    ticket's Attachments section (from get_issue) lists a screenshot or other
    image relevant to the task, e.g. a UI bug report where the screenshot
    shows the actual broken state.

    Only images are supported (png, jpeg, gif, webp) up to 5 MB; videos and
    other file types are rejected with an explanatory error.

    Args:
        attachment_id: The numeric attachment ID exactly as listed in
            get_issue output, e.g. get_attachment(attachment_id="107474").
            NOT the issue key and NOT the filename.
    """
    try:
        return await _get_attachment_impl(_get_client(), attachment_id)
    except (JiraError, ConfigError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
async def whoami() -> str:
    """Report which Atlassian account this server is authenticated as.

    Returns display name, email, account ID, and which base URL was selected
    (site URL vs cloud-ID fallback). Call this first when any other tool
    returns authentication or permission errors — it isolates whether the
    problem is the credentials or the specific resource.
    """
    try:
        return await _whoami_impl(_get_client())
    except (JiraError, ConfigError) as exc:
        raise ToolError(str(exc)) from exc


# -- --check mode ----------------------------------------------------------------


async def _run_check(issue_key: str | None) -> int:
    """Diagnostic mode for terminals and the PyCharm debugger.

    Runs entirely outside MCP, so printing to stdout is safe here.
    """
    print(f".env file: {'found' if ENV_FILE.is_file() else 'NOT found'} at {ENV_FILE}")
    try:
        settings: Settings = load_settings()
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}")
        return 1
    print(f"Site URL: {settings.site_url}")
    print(f"Cloud ID override: {settings.cloud_id or '(not set — auto-detect)'}")

    client = JiraClient(settings)
    try:
        print(await _whoami_impl(client))
        print(f"Cloud-ID fallback used: {client.used_cloud_id_fallback}")
        if issue_key:
            print(f"\n--- {issue_key} ---\n")
            print(await _get_issue_impl(client, issue_key, include_comments=True))
            print("\n--- context ---\n")
            print(await _get_issue_context_impl(client, issue_key))
    except (JiraError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1
    finally:
        await client.aclose()
    print("\nAll checks passed.")
    return 0


def main() -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        prog="jira_mcp",
        description="Read-only Jira MCP server (stdio). Use --check to diagnose outside MCP.",
    )
    parser.add_argument(
        "--check",
        nargs="?",
        const="",
        default=None,
        metavar="ISSUE_KEY",
        help="Run connectivity diagnostics and exit; optionally fetch ISSUE_KEY in full.",
    )
    args = parser.parse_args()

    if args.check is not None:
        # asyncio.run() creates an event loop, runs one coroutine, and cleans
        # up — the standard way to call async code from a sync entry point.
        sys.exit(asyncio.run(_run_check(args.check or None)))

    mcp.run()  # stdio transport is FastMCP's default


if __name__ == "__main__":
    main()
