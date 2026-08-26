"""Tests for issue-key normalization and Markdown rendering."""

from typing import Any

import pytest

from jira_mcp.render import (
    format_size,
    normalize_issue_key,
    render_comments,
    render_context,
    render_issue,
    render_search_results,
)


def test_normalize_lowercase_and_whitespace() -> None:
    assert normalize_issue_key("  proj-123 ") == "PROJ-123"


@pytest.mark.parametrize("bad", ["", "PROJ", "123", "PROJ 123", "-1", "PROJ-"])
def test_normalize_rejects_non_keys(bad: str) -> None:
    with pytest.raises(ValueError, match="issue key"):
        normalize_issue_key(bad)


def _issue(key: str = "PROJ-1", **field_overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "summary": "Fix the login flow",
        "status": {"name": "In Progress"},
        "issuetype": {"name": "Bug"},
        "priority": {"name": "High"},
        "resolution": None,
        "assignee": {"displayName": "Ada"},
        "reporter": {"displayName": "Grace"},
        "labels": ["auth"],
        "components": [{"name": "web"}],
        "fixVersions": [],
        "created": "2026-01-01T10:00:00.000+0000",
        "updated": "2026-01-02T10:00:00.000+0000",
        "description": {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Broken."}]}
            ],
        },
    }
    fields.update(field_overrides)
    return {"key": key, "fields": fields}


def test_render_issue_core_fields() -> None:
    output = render_issue(_issue())
    assert "# PROJ-1: Fix the login flow" in output
    assert "**Status:** In Progress" in output
    assert "**Resolution:** Unresolved" in output
    assert "**Assignee:** Ada" in output
    assert "Broken." in output
    assert "## Comments" not in output  # comments=None omits the section


def test_render_issue_custom_fields() -> None:
    issue = _issue(
        customfield_10001=5.0,
        customfield_10002={"value": "iOS"},
        customfield_10003={
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Given a card"}],
                },
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Then tags show"}],
                },
            ],
        },
        customfield_10004=None,
        customfield_10005="",
        customfield_10006="0|hzzzzz:",  # Rank noise
        customfield_10007=[{"value": "web"}, {"value": "app"}],
        customfield_10008={"unknown": "shape"},
        customfield_10009="unmapped",
    )
    names = {
        "customfield_10001": "Story Points",
        "customfield_10002": "Platform",
        "customfield_10003": "Acceptance Criteria",
        "customfield_10004": "Empty A",
        "customfield_10005": "Empty B",
        "customfield_10006": "Rank",
        "customfield_10007": "Channels",
        "customfield_10008": "Exotic",
    }
    output = render_issue(issue, field_names=names)
    assert "## Custom fields" in output
    assert "- **Story Points:** 5" in output
    assert "- **Platform:** iOS" in output
    assert "- **Channels:** web, app" in output
    # Multi-line rich text renders as its own block, not a bullet.
    assert "**Acceptance Criteria:**\n\nGiven a card\n\nThen tags show" in output
    # Empty, noise, and unrenderable fields are skipped silently.
    assert "Empty A" not in output
    assert "Rank" not in output
    assert "Exotic" not in output
    # Without a catalog entry, the raw ID is still shown rather than dropped.
    assert "- **customfield_10009:** unmapped" in output


def test_render_issue_without_custom_fields_omits_section() -> None:
    output = render_issue(_issue(), field_names={})
    assert "## Custom fields" not in output


def test_render_issue_lists_attachments() -> None:
    issue = _issue(
        attachment=[
            {
                "id": "10023",
                "filename": "login-broken.png",
                "mimeType": "image/png",
                "size": 240_640,
                "author": {"displayName": "Grace"},
            },
            {
                "id": "10024",
                "filename": "session.har",
                "mimeType": "application/json",
                "size": 512,
            },
        ]
    )
    output = render_issue(issue)
    assert "## Attachments (2)" in output
    assert "- **login-broken.png** (id 10023, image/png, 235 KB, by Grace)" in output
    assert "- **session.har** (id 10024, application/json, 512 B)" in output
    # The hint spells out the exact call for the first *image* attachment.
    assert 'get_attachment(attachment_id="10023")' in output


def test_render_issue_without_attachments_omits_section() -> None:
    output = render_issue(_issue())
    assert "## Attachments" not in output


@pytest.mark.parametrize(
    ("num_bytes", "expected"),
    [(0, "0 B"), (512, "512 B"), (240_640, "235 KB"), (5 * 1024 * 1024, "5.0 MB")],
)
def test_format_size(num_bytes: int, expected: str) -> None:
    assert format_size(num_bytes) == expected


def test_render_issue_with_empty_comments() -> None:
    output = render_issue(_issue(), comments=[])
    assert "## Comments (0)" in output


def test_render_comments_author_edit_and_visibility() -> None:
    comments = [
        {
            "author": {"displayName": "Ada"},
            "created": "2026-01-01T10:00:00.000+0000",
            "updated": "2026-01-05T10:00:00.000+0000",
            "visibility": {"type": "role", "value": "Developers"},
            "body": {
                "type": "doc",
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "hi"}]}
                ],
            },
        }
    ]
    output = render_comments(comments)
    assert "Ada — 2026-01-01" in output
    assert "edited 2026-01-05" in output
    assert "[visible to role: Developers]" in output
    assert "hi" in output


def test_render_context_link_direction() -> None:
    issue = _issue(
        parent={
            "key": "PROJ-100",
            "fields": {
                "summary": "Epic",
                "issuetype": {"name": "Epic"},
                "status": {"name": "Open"},
            },
        },
        subtasks=[
            {
                "key": "PROJ-2",
                "fields": {
                    "summary": "Sub",
                    "issuetype": {"name": "Sub-task"},
                    "status": {"name": "Done"},
                },
            }
        ],
        issuelinks=[
            {
                "type": {"inward": "is blocked by", "outward": "blocks"},
                "outwardIssue": {
                    "key": "PROJ-3",
                    "fields": {
                        "summary": "Downstream",
                        "issuetype": {"name": "Task"},
                        "status": {"name": "Open"},
                    },
                },
            },
            {
                "type": {"inward": "is blocked by", "outward": "blocks"},
                "inwardIssue": {
                    "key": "PROJ-4",
                    "fields": {
                        "summary": "Upstream",
                        "issuetype": {"name": "Task"},
                        "status": {"name": "Open"},
                    },
                },
            },
        ],
    )
    output = render_context(issue, epic_children=None)
    assert "**PROJ-100** [Epic, Open] Epic" in output
    assert "- **PROJ-2** [Sub-task, Done] Sub" in output
    assert "- blocks **PROJ-3**" in output
    assert "- is blocked by **PROJ-4**" in output
    assert "Issues in this epic" not in output


def test_render_context_epic_children_section() -> None:
    output = render_context(_issue(), epic_children=[])
    assert "## Issues in this epic (0)" in output


def test_render_search_results_and_truncation_note() -> None:
    issues = [_issue("PROJ-9")]
    output = render_search_results("project = PROJ", issues, has_more=True)
    assert "**PROJ-9**" in output
    assert "more results exist" in output
    empty = render_search_results("project = X", [], has_more=False)
    assert "no matching issues" in empty
