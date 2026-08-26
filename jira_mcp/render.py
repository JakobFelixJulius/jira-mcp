"""Render Jira API payloads as token-efficient Markdown.

Raw Jira JSON is enormous and mostly noise; these functions keep only what a
reader (human or model) needs. All of them accept the already-parsed JSON
dicts returned by JiraClient.
"""

from __future__ import annotations

import re
from typing import Any

from .adf import adf_to_markdown

_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")


def normalize_issue_key(raw: str) -> str:
    """Uppercase and trim an issue key so e.g. ' proj-123 ' works."""
    key = raw.strip().upper()
    if not _ISSUE_KEY_RE.match(key):
        raise ValueError(
            f"'{raw}' does not look like a Jira issue key (expected e.g. PROJ-123)."
        )
    return key


def format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.0f} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def _name(obj: Any, field: str = "name") -> str:
    """Safely pull a display name out of a nullable nested object."""
    if isinstance(obj, dict):
        return str(obj.get(field) or obj.get("displayName") or "")
    return ""


def _names(items: Any, field: str = "name") -> str:
    if not isinstance(items, list) or not items:
        return ""
    return ", ".join(_name(item, field) for item in items)


def _issue_line(issue: dict[str, Any]) -> str:
    """One-line summary: KEY [Type, Status] Summary — used everywhere a
    referenced issue is listed, so the reader can decide what to open next."""
    fields = issue.get("fields") or {}
    parts = [
        _name(fields.get("issuetype")),
        _name(fields.get("status")),
    ]
    meta = ", ".join(p for p in parts if p)
    return f"**{issue.get('key', '?')}** [{meta}] {fields.get('summary', '')}".rstrip()


def render_issue(
    issue: dict[str, Any],
    comments: list[dict[str, Any]] | None = None,
    field_names: dict[str, str] | None = None,
) -> str:
    fields = issue.get("fields") or {}
    lines = [f"# {issue.get('key', '?')}: {fields.get('summary', '')}", ""]

    facts = [
        ("Type", _name(fields.get("issuetype"))),
        ("Status", _name(fields.get("status"))),
        ("Priority", _name(fields.get("priority"))),
        ("Resolution", _name(fields.get("resolution")) or "Unresolved"),
        ("Assignee", _name(fields.get("assignee"), "displayName") or "Unassigned"),
        ("Reporter", _name(fields.get("reporter"), "displayName")),
        ("Labels", ", ".join(fields.get("labels") or [])),
        ("Components", _names(fields.get("components"))),
        ("Fix versions", _names(fields.get("fixVersions"))),
        ("Created", str(fields.get("created") or "")),
        ("Updated", str(fields.get("updated") or "")),
    ]
    lines.extend(f"- **{label}:** {value}" for label, value in facts if value)

    description = adf_to_markdown(fields.get("description"))
    lines.extend(["", "## Description", "", description or "*(no description)*"])

    custom_section = _render_custom_fields(fields, field_names or {})
    if custom_section:
        lines.extend(["", custom_section])

    attachments = fields.get("attachment") or []
    if attachments:
        lines.extend(["", f"## Attachments ({len(attachments)})", ""])
        lines.extend(_attachment_line(a) for a in attachments)
        hint = _attachment_hint(attachments)
        if hint:
            lines.extend(["", hint])

    if comments is not None:
        lines.extend(["", f"## Comments ({len(comments)})", ""])
        lines.append(render_comments(comments) if comments else "*(no comments)*")

    return "\n".join(lines)


# Machine-managed custom fields whose values are meaningless to a reader
# (Lexorank strings, dev-panel JSON blobs).
_NOISE_CUSTOM_FIELDS = frozenset({"rank", "development"})


def _custom_field_value(value: Any) -> str:
    """Best-effort rendering of a custom field value of unknown shape.

    Unknown structures render as "" and are skipped, so a new exotic field
    type never dumps raw JSON into the output.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        parts = [part for part in (_custom_field_value(v) for v in value) if part]
        return ", ".join(parts)
    if isinstance(value, dict):
        if value.get("type") == "doc":  # rich-text fields arrive as ADF
            return adf_to_markdown(value)
        # Option / user / version / sprint objects all carry one of these.
        for key in ("displayName", "value", "name"):
            if value.get(key):
                return str(value[key])
    return ""


def _render_custom_fields(fields: dict[str, Any], field_names: dict[str, str]) -> str:
    items: list[tuple[str, str]] = []
    for field_id, value in fields.items():
        if not field_id.startswith("customfield_"):
            continue
        name = field_names.get(field_id, field_id)
        if name.lower() in _NOISE_CUSTOM_FIELDS:
            continue
        rendered = _custom_field_value(value)
        if rendered:
            items.append((name, rendered))
    if not items:
        return ""
    items.sort(key=lambda item: item[0].lower())
    # Single-line values as facts; multi-line values (e.g. acceptance
    # criteria) as their own blocks below, where Markdown survives intact.
    bullets = [f"- **{name}:** {text}" for name, text in items if "\n" not in text]
    blocks = [f"**{name}:**\n\n{text}" for name, text in items if "\n" in text]
    parts = ["## Custom fields"]
    if bullets:
        parts.append("\n".join(bullets))
    parts.extend(blocks)
    return "\n\n".join(parts)


def _attachment_hint(attachments: list[dict[str, Any]]) -> str:
    """Spell out the exact fetch call for the first image, so the model's
    first get_attachment attempt already has the right parameter shape."""
    image_ids = [
        str(a.get("id"))
        for a in attachments
        if str(a.get("mimeType") or "").startswith("image/") and a.get("id")
    ]
    if not image_ids:
        return ""
    return (
        "*(fetch images for visual analysis with the numeric id, e.g. "
        f'get_attachment(attachment_id="{image_ids[0]}"))*'
    )


def _attachment_line(attachment: dict[str, Any]) -> str:
    filename = str(attachment.get("filename") or "?")
    mime_type = str(attachment.get("mimeType") or "unknown type")
    size = format_size(int(attachment.get("size") or 0))
    author = _name(attachment.get("author"), "displayName")
    by = f", by {author}" if author else ""
    return f"- **{filename}** (id {attachment.get('id', '?')}, {mime_type}, {size}{by})"


def render_comments(comments: list[dict[str, Any]]) -> str:
    if not comments:
        return "*(no comments)*"
    blocks = []
    for comment in comments:
        author = _name(comment.get("author"), "displayName") or "Unknown"
        created = str(comment.get("created") or "")
        edited = ""
        if comment.get("updated") and comment.get("updated") != comment.get("created"):
            edited = f" (edited {comment['updated']})"
        visibility = ""
        vis = comment.get("visibility")
        if isinstance(vis, dict):
            visibility = f" [visible to {vis.get('type', '')}: {vis.get('value', '')}]"
        body = adf_to_markdown(comment.get("body"))
        blocks.append(f"### {author} — {created}{edited}{visibility}\n\n{body}")
    return "\n\n".join(blocks)


def render_context(
    issue: dict[str, Any], epic_children: list[dict[str, Any]] | None
) -> str:
    fields = issue.get("fields") or {}
    lines = [f"# Context for {_issue_line(issue)}", ""]

    parent = fields.get("parent")
    lines.append("## Parent")
    lines.append(_issue_line(parent) if isinstance(parent, dict) else "*(none)*")

    subtasks = fields.get("subtasks") or []
    lines.extend(["", f"## Subtasks ({len(subtasks)})"])
    lines.extend(f"- {_issue_line(s)}" for s in subtasks)
    if not subtasks:
        lines.append("*(none)*")

    links = fields.get("issuelinks") or []
    lines.extend(["", f"## Linked issues ({len(links)})"])
    rendered_links = 0
    for link in links:
        link_type = link.get("type") or {}
        # Direction matters: an outwardIssue is described by the outward
        # phrase ("blocks"); an inwardIssue by the inward one ("is blocked by").
        if "outwardIssue" in link:
            relation, other = (
                link_type.get("outward", "relates to"),
                link["outwardIssue"],
            )
        elif "inwardIssue" in link:
            relation, other = link_type.get("inward", "relates to"), link["inwardIssue"]
        else:
            continue
        lines.append(f"- {relation} {_issue_line(other)}")
        rendered_links += 1
    if not rendered_links:
        lines.append("*(none)*")

    if epic_children is not None:
        lines.extend(["", f"## Issues in this epic ({len(epic_children)})"])
        lines.extend(f"- {_issue_line(c)}" for c in epic_children)
        if not epic_children:
            lines.append("*(none)*")

    return "\n".join(lines)


def render_search_results(
    jql: str, issues: list[dict[str, Any]], has_more: bool
) -> str:
    lines = [f"# Search results for `{jql}` ({len(issues)} shown)", ""]
    if not issues:
        lines.append("*(no matching issues)*")
    for issue in issues:
        fields = issue.get("fields") or {}
        assignee = _name(fields.get("assignee"), "displayName") or "Unassigned"
        lines.append(
            f"- {_issue_line(issue)} — {assignee}, updated {fields.get('updated', '?')}"
        )
    if has_more:
        lines.extend(["", "*(more results exist — refine the JQL or raise the limit)*"])
    return "\n".join(lines)


def render_myself(account: dict[str, Any], base_url: str, used_fallback: bool) -> str:
    token_kind = (
        "scoped token (api.atlassian.com via cloud-ID fallback)"
        if used_fallback
        else "direct site URL (unscoped token or cloud-ID override)"
    )
    return "\n".join(
        [
            f"**Account:** {account.get('displayName', '?')}",
            f"**Email:** {account.get('emailAddress', '?')}",
            f"**Account ID:** {account.get('accountId', '?')}",
            f"**Active:** {account.get('active', '?')}",
            f"**Base URL:** {base_url}",
            f"**Auth path:** {token_kind}",
        ]
    )
