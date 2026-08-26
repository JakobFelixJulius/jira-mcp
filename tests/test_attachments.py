"""Tests for the get_attachment guards: mime allowlist, 5 MB cap, ID checks."""

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from jira_mcp.client import JiraClient
from jira_mcp.config import Settings
from jira_mcp.server import MAX_ATTACHMENT_BYTES, _get_attachment_impl

SITE = "https://example.atlassian.net"
ACCOUNT = {"accountId": "abc", "displayName": "Ada"}
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"x" * 64


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> JiraClient:
    settings = Settings(
        email="ada@x.io",
        api_token="secret",
        site_url=SITE,
        cloud_id=None,
        env_file_found=True,
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JiraClient(settings, http=http)


class AttachmentHandler:
    """Serves auth, attachment metadata, and attachment content."""

    def __init__(self, meta: dict[str, Any], content: bytes = PNG_BYTES) -> None:
        self.meta = meta
        self.content = content
        self.content_requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{SITE}/rest/api/3/myself":
            return httpx.Response(200, json=ACCOUNT)
        if url == f"{SITE}/rest/api/3/attachment/{self.meta['id']}":
            return httpx.Response(200, json=self.meta)
        if url == f"{SITE}/rest/api/3/attachment/content/{self.meta['id']}":
            self.content_requests += 1
            return httpx.Response(
                200,
                content=self.content,
                headers={"Content-Type": str(self.meta.get("mimeType", ""))},
            )
        return httpx.Response(404)


def png_meta(**overrides: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "id": "10023",
        "filename": "screenshot.png",
        "mimeType": "image/png",
        "size": len(PNG_BYTES),
    }
    meta.update(overrides)
    return meta


async def test_image_attachment_is_returned() -> None:
    client = make_client(AttachmentHandler(png_meta()))
    image = await _get_attachment_impl(client, "10023")
    assert image.data == PNG_BYTES
    assert image._mime_type == "image/png"


async def test_attachment_id_is_trimmed() -> None:
    client = make_client(AttachmentHandler(png_meta()))
    image = await _get_attachment_impl(client, " 10023 ")
    assert image.data == PNG_BYTES


async def test_jpeg_mime_maps_to_image_jpeg() -> None:
    meta = png_meta(filename="shot.jpg", mimeType="image/jpeg")
    client = make_client(AttachmentHandler(meta))
    image = await _get_attachment_impl(client, "10023")
    assert image._mime_type == "image/jpeg"


async def test_integer_id_is_accepted() -> None:
    # Models often send the ID as a JSON number; that must not cost a retry.
    client = make_client(AttachmentHandler(png_meta()))
    image = await _get_attachment_impl(client, 10023)
    assert image.data == PNG_BYTES


@pytest.mark.parametrize("bad_id", ["", "abc def", "10023; DROP"])
async def test_garbage_id_is_rejected(bad_id: str) -> None:
    client = make_client(AttachmentHandler(png_meta()))
    with pytest.raises(ValueError, match="attachment ID"):
        await _get_attachment_impl(client, bad_id)


async def test_issue_key_error_lists_the_valid_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{SITE}/rest/api/3/myself":
            return httpx.Response(200, json=ACCOUNT)
        if url.startswith(f"{SITE}/rest/api/3/issue/PROJ-1"):
            return httpx.Response(
                200, json={"fields": {"attachment": [png_meta(), png_meta(id="99")]}}
            )
        return httpx.Response(404)

    client = make_client(handler)
    with pytest.raises(ValueError, match="issue key, not an attachment ID") as exc:
        await _get_attachment_impl(client, "proj-1")
    assert "screenshot.png = id 10023" in str(exc.value)
    assert "id 99" in str(exc.value)


async def test_issue_key_without_attachments_says_so() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{SITE}/rest/api/3/myself":
            return httpx.Response(200, json=ACCOUNT)
        if url.startswith(f"{SITE}/rest/api/3/issue/PROJ-1"):
            return httpx.Response(200, json={"fields": {"attachment": []}})
        return httpx.Response(404)

    client = make_client(handler)
    with pytest.raises(ValueError, match="no attachments"):
        await _get_attachment_impl(client, "PROJ-1")


@pytest.mark.parametrize(
    ("filename", "mime"),
    [
        ("demo.mp4", "video/mp4"),
        ("demo.mov", "video/quicktime"),
        ("spec.pdf", "application/pdf"),
        ("logs.zip", "application/zip"),
        ("unknown.bin", ""),
    ],
)
async def test_non_image_types_are_rejected_without_download(
    filename: str, mime: str
) -> None:
    handler = AttachmentHandler(png_meta(filename=filename, mimeType=mime))
    client = make_client(handler)
    with pytest.raises(ValueError, match="cannot be analysed visually"):
        await _get_attachment_impl(client, "10023")
    assert handler.content_requests == 0


async def test_oversized_metadata_is_rejected_without_download() -> None:
    handler = AttachmentHandler(png_meta(size=MAX_ATTACHMENT_BYTES + 1))
    client = make_client(handler)
    with pytest.raises(ValueError, match="5.0 MB limit"):
        await _get_attachment_impl(client, "10023")
    assert handler.content_requests == 0


async def test_oversized_actual_content_is_rejected() -> None:
    # Metadata claims a small size but the real payload is over the cap.
    handler = AttachmentHandler(
        png_meta(size=100), content=b"x" * (MAX_ATTACHMENT_BYTES + 1)
    )
    client = make_client(handler)
    with pytest.raises(ValueError, match="5.0 MB limit"):
        await _get_attachment_impl(client, "10023")


async def test_empty_content_is_rejected() -> None:
    handler = AttachmentHandler(png_meta(size=0), content=b"")
    client = make_client(handler)
    with pytest.raises(ValueError, match="no content"):
        await _get_attachment_impl(client, "10023")
