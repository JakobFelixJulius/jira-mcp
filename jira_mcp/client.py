"""Read-only Jira HTTP client.

Two invariants live here:

1. **Read-only enforcement.** Every request funnels through ``_request``,
   which permits only GET — plus the single explicit exception
   ``POST /rest/api/3/search/jql`` (a read that Atlassian requires as POST).
   Any other method/path combination raises ``ReadOnlyViolationError``, so a
   future edit that adds a write call fails loudly instead of quietly working.

2. **Base URL auto-detection.** Unscoped API tokens work against
   ``https://<site>.atlassian.net``; scoped tokens fail *silently* there
   (anonymous-looking responses) and must use
   ``https://api.atlassian.com/ex/jira/{cloudId}``. We probe at startup,
   fall back via ``{site}/_edge/tenant_info``, cache the winner for the
   process lifetime, and log the decision to stderr.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import Settings

logger = logging.getLogger(__name__)

MYSELF_PATH = "/rest/api/3/myself"

# The complete set of non-GET requests this client will ever make.
ALLOWED_POST_PATHS = frozenset({"/rest/api/3/search/jql"})

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class ReadOnlyViolationError(RuntimeError):
    """A code path attempted a request that could modify Jira."""


class JiraError(Exception):
    """An API call failed; the message is written to be actionable."""


class BaseUrlDetectionError(JiraError):
    """Neither candidate base URL authenticated successfully."""


class JiraClient:
    def __init__(
        self, settings: Settings, http: httpx.AsyncClient | None = None
    ) -> None:
        self._settings = settings
        # Basic auth (email:token); the token itself is never logged.
        self._http = http or httpx.AsyncClient(
            auth=(settings.email, settings.api_token),
            timeout=DEFAULT_TIMEOUT,
            headers={"Accept": "application/json"},
            follow_redirects=True,
        )
        self._base_url: str | None = None
        # Diagnostics for --check; filled in by _ensure_base_url().
        self.used_cloud_id_fallback: bool = False
        self.account: dict[str, Any] | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def base_url(self) -> str | None:
        return self._base_url

    # -- public request helpers ------------------------------------------------

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._request("GET", path, params=params)
        return response.json()

    async def search_jql(self, body: dict[str, Any]) -> Any:
        response = await self._request("POST", "/rest/api/3/search/jql", json_body=body)
        return response.json()

    # -- the single funnel -----------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        method = method.upper()
        if method != "GET" and not (method == "POST" and path in ALLOWED_POST_PATHS):
            raise ReadOnlyViolationError(
                f"Blocked {method} {path}: this server is strictly read-only. "
                f"Only GET requests are permitted, plus POST to {sorted(ALLOWED_POST_PATHS)}."
            )
        base = await self._ensure_base_url()
        response = await self._http.request(
            method, f"{base}{path}", params=params, json=json_body
        )
        self._raise_for_status(response, path)
        return response

    # -- base URL detection ----------------------------------------------------

    async def _ensure_base_url(self) -> str:
        if self._base_url is not None:
            return self._base_url

        if self._settings.cloud_id:
            base = f"https://api.atlassian.com/ex/jira/{self._settings.cloud_id}"
            account = await self._probe(base)
            if account is None:
                raise BaseUrlDetectionError(
                    f"ATLASSIAN_CLOUD_ID is set, but authentication against {base} failed. "
                    "Check the cloud ID, ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN."
                )
            logger.info(
                "Base URL: %s (ATLASSIAN_CLOUD_ID override, no detection)", base
            )
            return self._select(base, account, fallback=False)

        site = self._settings.site_url
        account = await self._probe(site)
        if account is not None:
            logger.info(
                "Base URL: %s (site URL authenticated directly — unscoped token)", site
            )
            return self._select(site, account, fallback=False)

        logger.info(
            "Site URL %s did not authenticate (typical for scoped tokens); "
            "fetching cloud ID from _edge/tenant_info",
            site,
        )
        cloud_id = await self._fetch_cloud_id(site)
        base = f"https://api.atlassian.com/ex/jira/{cloud_id}"
        account = await self._probe(base)
        if account is None:
            raise BaseUrlDetectionError(
                f"Authentication failed against both {site} and {base}. "
                "Check ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN. If the token was recently "
                "created, confirm it has the read:jira-work and read:jira-user scopes."
            )
        logger.info("Base URL: %s (cloud-ID fallback — scoped token)", base)
        return self._select(base, account, fallback=True)

    def _select(self, base: str, account: dict[str, Any], fallback: bool) -> str:
        self._base_url = base
        self.account = account
        self.used_cloud_id_fallback = fallback
        logger.info(
            "Authenticated as %s (%s)",
            account.get("displayName", "?"),
            account.get("emailAddress", account.get("accountId", "?")),
        )
        return base

    async def _probe(self, base: str) -> dict[str, Any] | None:
        """GET /myself against a candidate base URL.

        Returns the account JSON only for a real authenticated account: a
        scoped token hitting the wrong base can produce 200 responses that
        merely *look* anonymous, so a 200 without an accountId is a failure.
        """
        try:
            response = await self._http.get(f"{base}{MYSELF_PATH}")
        except httpx.HTTPError as exc:
            logger.info("Probe of %s failed: %s", base, exc)
            return None
        if response.status_code != 200:
            logger.info("Probe of %s returned HTTP %s", base, response.status_code)
            return None
        try:
            data = response.json()
        except ValueError:
            logger.info("Probe of %s returned non-JSON", base)
            return None
        if not isinstance(data, dict) or not data.get("accountId"):
            logger.info("Probe of %s returned an anonymous-looking response", base)
            return None
        return data

    async def _fetch_cloud_id(self, site: str) -> str:
        """Resolve the cloud ID from the site's tenant info endpoint.

        Note: _edge/tenant_info is not part of Atlassian's formally supported
        REST API (though Atlassian support docs tell users to call it), hence
        the explicit escape hatch in the error message.
        """
        try:
            response = await self._http.get(f"{site}/_edge/tenant_info")
            response.raise_for_status()
            cloud_id = response.json().get("cloudId")
        except (httpx.HTTPError, ValueError) as exc:
            raise BaseUrlDetectionError(
                f"Could not determine the cloud ID from {site}/_edge/tenant_info ({exc}). "
                "Set ATLASSIAN_CLOUD_ID manually in .env to skip detection."
            ) from exc
        if not cloud_id:
            raise BaseUrlDetectionError(
                f"{site}/_edge/tenant_info returned no cloudId. "
                "Set ATLASSIAN_CLOUD_ID manually in .env to skip detection."
            )
        return str(cloud_id)

    # -- error mapping ---------------------------------------------------------

    def _raise_for_status(self, response: httpx.Response, path: str) -> None:
        status = response.status_code
        if status < 400:
            return
        if status == 401:
            raise JiraError(
                "401 Unauthorized: Jira rejected the credentials. Check ATLASSIAN_EMAIL "
                "and ATLASSIAN_API_TOKEN (tokens expire and can be revoked)."
            )
        if status == 403:
            raise JiraError(
                "403 Forbidden: the token or account lacks access. If this is a scoped "
                "token, confirm it has the read:jira-work and read:jira-user scopes."
            )
        if status == 404:
            raise JiraError(
                f"404 Not Found for {path}: the resource does not exist — or your account "
                "lacks permission to see it. Jira reports issues you cannot view as 404, "
                "and an API token never grants more access than the account it belongs to."
            )
        if status == 429:
            raise JiraError(
                "429 Too Many Requests: Jira is rate-limiting; retry shortly."
            )
        raise JiraError(
            f"Jira returned HTTP {status} for {path}: {response.text[:300]}"
        )
