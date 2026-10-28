"""The MCP server: tool definitions, --check CLI mode, entry point.

stdio discipline: stdout carries only MCP protocol traffic, so nothing here
may print() in server mode. All diagnostics go to stderr via logging.
(--check mode runs outside MCP entirely and prints freely.)
"""

from __future__ import annotations

import argparse
import asyncio
import codecs
import logging
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.utilities.types import Image

from .client import MYSELF_PATH, JiraClient, JiraError
from .config import ENV_FILE, ConfigError, Settings, load_settings
from .render import (
    format_size,
    is_text_attachment,
    needs_content_sniff,
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
        "lists attachments relevant to the task (e.g. a screenshot on a UI "
        "bug, or a log, JSON payload, or CSV export), fetch them with "
        "get_attachment to read them. Call whoami only to debug "
        "authentication.\n"
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


# *all so custom fields (acceptance criteria etc.) come along; comment and
# worklog are excluded because comments are fetched separately with ordering.
ISSUE_FIELDS = "*all,-comment,-worklog"
CONTEXT_FIELDS = "summary,status,issuetype,parent,subtasks,issuelinks"
SEARCH_FIELDS = ["summary", "status", "issuetype", "priority", "assignee", "updated"]

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024

# The only mime types Claude can analyse visually, mapped to the format name
# FastMCP's Image helper expects. Besides these, only text files are served;
# known binaries (videos, PDFs, archives) are rejected before download.
IMAGE_FORMATS = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
}

# Text up to this size is returned inline (well under Claude Code's ~25k token
# MCP output limit). Longer text is saved in full to ATTACHMENT_CACHE_DIR and
# only a head/tail preview plus the file path is returned, so the model can
# grep or page through it instead of loading it all into context.
MAX_INLINE_TEXT_CHARS = 20_000
PREVIEW_LINES = 50
PREVIEW_MAX_CHARS = 4_000
ATTACHMENT_CACHE_DIR = Path(tempfile.gettempdir()) / "jira-mcp" / "attachments"

# How much of an untyped file is inspected to decide whether it is text.
SNIFF_BYTES = 8192


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


# Field-name catalog (customfield_10123 -> "Acceptance Criteria"), fetched
# once per process. Only successful lookups are cached, so a transient
# failure is retried on the next call.
_field_names: dict[str, str] | None = None


async def _get_field_names(client: JiraClient) -> dict[str, str]:
    global _field_names
    if _field_names is None:
        try:
            catalog = await client.get_json("/rest/api/3/field")
        except JiraError as exc:
            logger.warning(
                "Field catalog unavailable; custom fields will show raw IDs: %s", exc
            )
            return {}
        _field_names = {
            str(field["id"]): str(field.get("name") or field["id"])
            for field in catalog
            if isinstance(field, dict) and field.get("id")
        }
    return _field_names


async def _get_issue_impl(
    client: JiraClient, issue_key: str, include_comments: bool
) -> str:
    key = normalize_issue_key(issue_key)
    field_names = await _get_field_names(client)
    issue = await client.get_json(
        f"/rest/api/3/issue/{key}", params={"fields": ISSUE_FIELDS}
    )
    comments = (
        await _fetch_comments(client, key, 100, False) if include_comments else None
    )
    return render_issue(issue, comments, field_names)


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


def _decode_text(data: bytes, filename: str) -> str:
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    if b"\x00" in data:
        raise ValueError(
            f"Attachment '{filename}' looks like a binary file despite its "
            "name or type, so it cannot be read as text."
        )
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _looks_like_text(data: bytes) -> bool:
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return True
    sample = data[:SNIFF_BYTES]
    if b"\x00" in sample:
        return False
    try:
        # Incremental, so a multi-byte character cut off at the sample end is fine.
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
    except UnicodeDecodeError:
        return False
    return True


def _save_text(att_id: str, filename: str, text: str) -> Path:
    safe_name = re.sub(r"[^\w.-]+", "_", Path(filename).name).strip("._") or "file"
    ATTACHMENT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = ATTACHMENT_CACHE_DIR / f"{att_id}-{safe_name}"
    # Always UTF-8 on disk, whatever the original encoding, so grep just works.
    path.write_text(text, encoding="utf-8", newline="")
    return path


def _preview(lines: list[str], from_end: bool = False) -> str:
    text = "\n".join(lines)
    if len(text) <= PREVIEW_MAX_CHARS:
        return text
    if from_end:
        return "[...]" + text[-PREVIEW_MAX_CHARS:]
    return text[:PREVIEW_MAX_CHARS] + "[...]"


def _render_saved_text(header: str, path: Path, text: str) -> str:
    lines = text.splitlines()
    head = _preview(lines[:PREVIEW_LINES])
    tail = _preview(lines[-PREVIEW_LINES:], from_end=True)
    return (
        f"{header}\n\n"
        f"Too long to return inline ({len(lines):,} lines, {len(text):,} "
        f"characters). The full text is saved at:\n{path}\n"
        "Search it with grep or read it in ranges (Read with offset/limit) "
        "instead of loading it all.\n\n"
        f"--- first {min(PREVIEW_LINES, len(lines))} lines ---\n{head}\n\n"
        f"--- last {min(PREVIEW_LINES, len(lines))} lines ---\n{tail}"
    )


async def _get_attachment_impl(
    client: JiraClient, attachment_id: str | int
) -> Image | str:
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

    # Metadata first: it carries mime type and size, so known binaries and
    # oversized files are rejected without downloading a single byte.
    meta = await client.get_json(f"/rest/api/3/attachment/{att_id}")
    filename = str(meta.get("filename") or att_id)
    mime_type = str(meta.get("mimeType") or "").lower()
    size = int(meta.get("size") or 0)

    image_format = IMAGE_FORMATS.get(mime_type)
    is_text = image_format is None and is_text_attachment(mime_type, filename)
    sniff = (
        image_format is None
        and not is_text
        and needs_content_sniff(mime_type, filename)
    )
    if image_format is None and not is_text and not sniff:
        supported = ", ".join(sorted(set(IMAGE_FORMATS)))
        raise ValueError(
            f"Attachment '{filename}' has type '{mime_type or 'unknown'}', which "
            f"cannot be read. Supported are images ({supported}) and text files "
            "(logs, JSON, XML, CSV, source code, ...). Videos, PDFs, archives "
            "and other binary files cannot be fetched."
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
    if image_format is not None:
        return Image(data=data, format=image_format)
    if sniff and not _looks_like_text(data):
        raise ValueError(
            f"Attachment '{filename}' has type '{mime_type or 'unknown'}' and its "
            "content is not text, so it cannot be read."
        )
    text = _decode_text(data, filename)
    header = f"Attachment: {filename} ({mime_type or 'unknown type'}, {format_size(len(data))})"
    if len(text) <= MAX_INLINE_TEXT_CHARS:
        return f"{header}\n\n{text}"
    try:
        path = _save_text(att_id, filename, text)
    except OSError as exc:
        raise ValueError(
            f"Attachment '{filename}' is too long to return inline and could "
            f"not be saved to {ATTACHMENT_CACHE_DIR}: {exc}"
        ) from exc
    return _render_saved_text(header, path, text)


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
    description, all non-empty custom fields (e.g. acceptance criteria,
    story points) with their display names, the attachment list (fetch
    images via get_attachment), and (by default) the complete comment
    thread.

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
async def get_attachment(attachment_id: str | int) -> Image | str:
    """Download an image or text file attached to a Jira issue.

    Images are returned as images so they can be looked at directly, e.g. a
    UI bug report where the screenshot shows the actual broken state. Text
    files (logs, stack traces, JSON/HAR, XML, YAML, CSV, source code, ...)
    are returned as their text content. Use it when a ticket's Attachments
    section (from get_issue) lists a file relevant to the task. Files of
    unknown type (e.g. Dockerfile, app.log.1) are checked by content.

    Files up to 5 MB are supported. Text longer than 20,000 characters is
    saved in full to a local file; the result then gives that file's path
    plus its first and last lines, so search it with grep or read it in
    ranges. Videos, PDFs, archives and other binary files are rejected with
    an explanatory error.

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
