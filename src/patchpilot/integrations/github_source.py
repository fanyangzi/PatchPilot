"""Read-only GitHub PR/Issue source resolution.

The resolver deliberately has no write capabilities.  It accepts only canonical
GitHub web URLs, uses the GitHub API host for metadata, and returns an explicit
``pending`` result when no token/configuration is available.  HTTP is injected
so the application and tests can choose a constrained transport without
coupling the domain to httpx or urllib.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
import json
import os
import re
import ssl
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPSHandler


_GITHUB_WEB_HOST = "github.com"
_GITHUB_API_HOST = "api.github.com"
_REF_RE = re.compile(r"^/(?P<owner>[A-Za-z0-9][A-Za-z0-9_.-]{0,99})/(?P<repo>[A-Za-z0-9_.-]{1,100})/(?P<kind>pull|issues)/(?P<number>[1-9][0-9]{0,8})/?$")
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class SourceResolutionError(Exception):
    """A safe, user-facing source resolution failure."""

    def __init__(self, code: str, message: str, *, retryable: bool = False, status_code: int | None = None):
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status_code = status_code
        super().__init__(message)


def _parse_repo_id(value: str) -> tuple[str, str]:
    """Validate an owner/name repository identifier for API path building."""
    if not isinstance(value, str) or value.count("/") != 1:
        raise SourceResolutionError("invalid_publication_target", "target repository must be owner/name")
    owner, repo = value.split("/", 1)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repo):
        raise SourceResolutionError("invalid_publication_target", "target repository must be owner/name")
    return owner, repo


@dataclass(frozen=True, slots=True)
class GitHubRef:
    owner: str
    repo: str
    kind: str  # ``pr`` or ``issue``
    number: int
    canonical_url: str
    api_url: str

    @property
    def repo_id(self) -> str:
        return f"{self.owner}/{self.repo}"


def _validate_public_host(host: str, field: str) -> None:
    host = (host or "").lower().rstrip(".")
    if not host or host in {"localhost", "localhost.localdomain"}:
        raise SourceResolutionError("ssrf_blocked", f"{field} cannot target localhost")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_reserved or address.is_multicast or address.is_unspecified
    ):
        raise SourceResolutionError("ssrf_blocked", f"{field} cannot target a private or local address")


def parse_github_url(value: str) -> GitHubRef:
    """Parse a GitHub PR/Issue web URL and map it to a GitHub API URL.

    Userinfo, ports, fragments and alternate hosts are rejected.  Query strings
    are ignored only when they are empty; this avoids accepting tracking URLs
    whose target is ambiguous.
    """
    if not isinstance(value, str) or not value:
        raise SourceResolutionError("invalid_source_url", "source URL is required")
    parsed = urlparse(value)
    if parsed.scheme.lower() != "https" or parsed.hostname is None:
        raise SourceResolutionError("invalid_source_url", "source URL must use https")
    if parsed.username or parsed.password:
        raise SourceResolutionError("invalid_source_url", "source URL cannot contain credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise SourceResolutionError("invalid_source_url", "source URL contains an invalid port") from exc
    if port is not None:
        raise SourceResolutionError("invalid_source_url", "source URL cannot contain a port")
    _validate_public_host(parsed.hostname, "source URL")
    if parsed.hostname.lower().rstrip(".") != _GITHUB_WEB_HOST:
        raise SourceResolutionError("invalid_source_url", "source URL must be hosted on github.com")
    if parsed.query or parsed.fragment:
        raise SourceResolutionError("invalid_source_url", "source URL cannot contain a query or fragment")
    match = _REF_RE.fullmatch(parsed.path)
    if not match:
        raise SourceResolutionError("invalid_source_url", "source URL must identify a GitHub pull request or issue")
    owner, repo = match.group("owner"), match.group("repo")
    kind = "pr" if match.group("kind") == "pull" else "issue"
    number = int(match.group("number"))
    canonical = f"https://github.com/{owner}/{repo}/{'pull' if kind == 'pr' else 'issues'}/{number}"
    api_kind = "pulls" if kind == "pr" else "issues"
    api_url = f"https://{_GITHUB_API_HOST}/repos/{owner}/{repo}/{api_kind}/{number}"
    return GitHubRef(owner, repo, kind, number, canonical, api_url)


@dataclass(frozen=True, slots=True)
class HTTPResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes
    url: str


class HTTPClient(Protocol):
    def get(self, url: str, *, headers: Mapping[str, str], timeout: float) -> HTTPResponse:
        ...

    def post(self, url: str, *, headers: Mapping[str, str], body: bytes, timeout: float) -> HTTPResponse:
        ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # pragma: no cover - urllib transport detail
        return None


class UrllibHTTPClient:
    """Small dependency-free HTTP client with redirects disabled.

    ``POST`` is intentionally available only to the explicit publication path;
    source resolution itself remains GET-only.  The host and response limits
    are applied to both methods so an injected transport cannot turn this
    integration into an arbitrary URL writer.
    """

    def __init__(self, *, max_bytes: int = _MAX_RESPONSE_BYTES):
        self.max_bytes = max_bytes
        self._context = ssl.create_default_context()
        self._opener = build_opener(_NoRedirect(), HTTPSHandler(context=self._context))

    def get(self, url: str, *, headers: Mapping[str, str], timeout: float) -> HTTPResponse:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != _GITHUB_API_HOST:
            raise SourceResolutionError("ssrf_blocked", "GitHub API requests must target api.github.com")
        _validate_public_host(parsed.hostname, "GitHub API URL")
        req = Request(url, method="GET", headers=dict(headers))
        try:
            with self._opener.open(req, timeout=timeout) as response:
                body = response.read(self.max_bytes + 1)
                if len(body) > self.max_bytes:
                    raise SourceResolutionError("response_too_large", "GitHub response exceeds the size limit")
                return HTTPResponse(int(response.status), dict(response.headers.items()), body, str(response.url))
        except HTTPError as exc:
            body = exc.read(self.max_bytes + 1)
            if len(body) > self.max_bytes:
                body = body[: self.max_bytes]
            return HTTPResponse(int(exc.code), dict(exc.headers.items()), body, str(exc.url))
        except URLError as exc:
            raise SourceResolutionError("source_unavailable", "GitHub source could not be reached", retryable=True) from exc

    def post(self, url: str, *, headers: Mapping[str, str], body: bytes, timeout: float) -> HTTPResponse:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != _GITHUB_API_HOST:
            raise SourceResolutionError("ssrf_blocked", "GitHub API requests must target api.github.com")
        _validate_public_host(parsed.hostname, "GitHub API URL")
        if len(body) > self.max_bytes:
            raise SourceResolutionError("request_too_large", "GitHub request exceeds the size limit")
        req_headers = dict(headers)
        req_headers.setdefault("Content-Type", "application/json")
        req = Request(url, method="POST", headers=req_headers, data=body)
        try:
            with self._opener.open(req, timeout=timeout) as response:
                response_body = response.read(self.max_bytes + 1)
                if len(response_body) > self.max_bytes:
                    raise SourceResolutionError("response_too_large", "GitHub response exceeds the size limit")
                return HTTPResponse(int(response.status), dict(response.headers.items()), response_body, str(response.url))
        except HTTPError as exc:
            response_body = exc.read(self.max_bytes + 1)
            if len(response_body) > self.max_bytes:
                response_body = response_body[: self.max_bytes]
            return HTTPResponse(int(exc.code), dict(exc.headers.items()), response_body, str(exc.url))
        except URLError as exc:
            raise SourceResolutionError("source_unavailable", "GitHub source could not be reached", retryable=True) from exc


@dataclass(frozen=True, slots=True)
class SourceResolution:
    status: str  # resolved | pending | unavailable | not_found | forbidden | invalid
    source: dict[str, Any]
    code: str | None = None
    message: str | None = None
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": self.status, "source": self.source}
        if self.code:
            result["code"] = self.code
        if self.message:
            result["message"] = self.message
        result["retryable"] = self.retryable
        return result


class GitHubSourceResolver:
    """Resolve one PR/Issue metadata snapshot through an injected read client."""

    def __init__(self, http_client: HTTPClient | None = None, *, token: str | None = None, timeout: float = 10.0, allow_anonymous: bool | None = None, allow_write: bool | None = None):
        self.http_client = http_client or UrllibHTTPClient()
        # Never include this value in a returned object.  It is read at call
        # time so environment changes do not require a process restart in tests.
        self.token = token if token is not None else os.getenv("GITHUB_TOKEN")
        self.timeout = max(1.0, min(float(timeout), 30.0))
        self.allow_anonymous = (allow_anonymous if allow_anonymous is not None else os.getenv("PATCHPILOT_GITHUB_ALLOW_ANONYMOUS", "0") == "1")
        # Writes are an opt-in capability.  A configured read token alone must
        # never be enough to create a comment or otherwise mutate GitHub.
        self.allow_write = (allow_write if allow_write is not None else os.getenv("PATCHPILOT_GITHUB_ALLOW_WRITE", "0") == "1")

    def _headers(self, *, user_agent: str) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": user_agent,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def _decode_response(response: HTTPResponse) -> dict[str, Any]:
        parsed_response = urlparse(response.url)
        if parsed_response.scheme != "https" or (parsed_response.hostname or "").lower().rstrip(".") != _GITHUB_API_HOST:
            raise SourceResolutionError("ssrf_blocked", "GitHub API redirected to an untrusted host")
        try:
            payload = json.loads(response.body.decode("utf-8")) if response.body else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceResolutionError("invalid_source_response", "GitHub returned malformed metadata", retryable=True) from exc
        if not isinstance(payload, dict):
            raise SourceResolutionError("invalid_source_response", "GitHub returned malformed metadata", retryable=True)
        return payload

    def _get_json(self, url: str, *, user_agent: str) -> tuple[HTTPResponse, dict[str, Any]]:
        try:
            response = self.http_client.get(url, headers=self._headers(user_agent=user_agent), timeout=self.timeout)
        except SourceResolutionError:
            raise
        except Exception as exc:
            raise SourceResolutionError("source_unavailable", "GitHub source could not be read", retryable=True) from exc
        return response, self._decode_response(response)

    def current_pull_request(self, repo_id: str, number: int) -> dict[str, Any]:
        """Read the current PR head and repository permission evidence.

        The return value is deliberately limited to publication-relevant
        fields.  ``head_sha`` is the value that must match the preview's
        expected head immediately before a write.
        """
        owner, repo = _parse_repo_id(repo_id)
        if number < 1:
            raise SourceResolutionError("invalid_publication_target", "pull request number must be positive")
        url = f"https://{_GITHUB_API_HOST}/repos/{owner}/{repo}/pulls/{number}"
        response, payload = self._get_json(url, user_agent="PatchPilot-publication-reader/1")
        if response.status_code in (401, 403):
            raise SourceResolutionError("github_forbidden", "GitHub did not authorize this source", status_code=response.status_code)
        if response.status_code == 404:
            raise SourceResolutionError("github_not_found", "GitHub pull request was not found or is not visible", status_code=404)
        if response.status_code >= 500:
            raise SourceResolutionError("github_unavailable", "GitHub returned a temporary server error", retryable=True, status_code=response.status_code)
        if response.status_code < 200 or response.status_code >= 300:
            raise SourceResolutionError("github_bad_response", "GitHub returned an unexpected response", retryable=True, status_code=response.status_code)
        head = payload.get("head") if isinstance(payload.get("head"), Mapping) else {}
        head_sha = str(head.get("sha") or payload.get("head_sha") or "")
        if not head_sha:
            raise SourceResolutionError("invalid_source_response", "GitHub pull request metadata lacks an exact head commit", retryable=True)
        permissions = payload.get("permissions") if isinstance(payload.get("permissions"), Mapping) else {}
        return {
            "repo_id": f"{owner}/{repo}",
            "number": number,
            "head_sha": head_sha,
            "base_sha": str((payload.get("base") or {}).get("sha") or "") if isinstance(payload.get("base"), Mapping) else "",
            "state": str(payload.get("state") or "unknown"),
            "permissions": dict(permissions),
            "url": f"https://github.com/{owner}/{repo}/pull/{number}",
        }

    def publish_report_comment(self, *, repo_id: str, number: int, expected_head: str, body: str) -> dict[str, Any]:
        """Publish one report comment after rechecking head and write access.

        This is the only mutating method in the source integration.  It is
        disabled by default and has no merge or branch-write operation.
        """
        if not self.allow_write:
            raise SourceResolutionError("github_write_disabled", "GitHub publication is disabled by default")
        if not self.token:
            raise SourceResolutionError("github_auth_not_configured", "GitHub write credentials are not configured")
        current = self.current_pull_request(repo_id, number)
        if current["head_sha"] != expected_head:
            raise SourceResolutionError("stale_head", "GitHub pull request head changed after preview", status_code=409)
        permissions = current.get("permissions") or {}
        if permissions.get("push") is not True and permissions.get("maintain") is not True and permissions.get("admin") is not True:
            # A PR response can omit permissions for some token types.  Query
            # repository metadata as a second, explicit permission check.
            owner, repo = _parse_repo_id(repo_id)
            response, payload = self._get_json(f"https://{_GITHUB_API_HOST}/repos/{owner}/{repo}", user_agent="PatchPilot-publication-reader/1")
            if response.status_code in (401, 403):
                raise SourceResolutionError("github_forbidden", "GitHub write permission was not granted", status_code=response.status_code)
            if response.status_code < 200 or response.status_code >= 300:
                raise SourceResolutionError("github_permission_unavailable", "GitHub write permission could not be verified", retryable=True, status_code=response.status_code)
            repo_permissions = payload.get("permissions") if isinstance(payload.get("permissions"), Mapping) else {}
            if repo_permissions.get("push") is not True and repo_permissions.get("maintain") is not True and repo_permissions.get("admin") is not True:
                raise SourceResolutionError("github_write_forbidden", "GitHub token does not have repository write permission", status_code=403)
        owner, repo = _parse_repo_id(repo_id)
        url = f"https://{_GITHUB_API_HOST}/repos/{owner}/{repo}/issues/{number}/comments"
        request_body = json.dumps({"body": body}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            response = self.http_client.post(url, headers=self._headers(user_agent="PatchPilot-publication-writer/1"), body=request_body, timeout=self.timeout)
        except SourceResolutionError:
            raise
        except Exception as exc:
            raise SourceResolutionError("source_unavailable", "GitHub publication could not be completed", retryable=True) from exc
        payload = self._decode_response(response)
        if response.status_code in (401, 403):
            raise SourceResolutionError("github_write_forbidden", "GitHub did not authorize publication", status_code=response.status_code)
        if response.status_code >= 500:
            raise SourceResolutionError("github_unavailable", "GitHub publication temporarily failed", retryable=True, status_code=response.status_code)
        if response.status_code < 200 or response.status_code >= 300:
            raise SourceResolutionError("github_publish_failed", "GitHub rejected publication", retryable=False, status_code=response.status_code)
        return {
            "provider": "github",
            "publication_type": "comment",
            "target_repo": f"{owner}/{repo}",
            "target_pr": number,
            "head_sha": expected_head,
            "external_id": str(payload.get("id") or ""),
            "url": str(payload.get("html_url") or ""),
        }

    def resolve(self, source_url: str) -> SourceResolution:
        ref = parse_github_url(source_url)
        if not self.token and not self.allow_anonymous:
            return SourceResolution(
                "pending", {"url": ref.canonical_url, "kind": ref.kind, "repo_id": ref.repo_id, "number": ref.number},
                "github_auth_not_configured", "GitHub read credentials are not configured", True,
            )
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "PatchPilot-source-reader/1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = self.http_client.get(ref.api_url, headers=headers, timeout=self.timeout)
        except SourceResolutionError:
            raise
        except Exception as exc:
            raise SourceResolutionError("source_unavailable", "GitHub source could not be read", retryable=True) from exc
        # A transport must not silently follow a redirect to another host.
        parsed_response = urlparse(response.url)
        if parsed_response.scheme != "https" or (parsed_response.hostname or "").lower().rstrip(".") != _GITHUB_API_HOST:
            raise SourceResolutionError("ssrf_blocked", "GitHub API redirected to an untrusted host")
        try:
            payload = json.loads(response.body.decode("utf-8")) if response.body else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceResolutionError("invalid_source_response", "GitHub returned malformed metadata", retryable=True) from exc
        if response.status_code in (401, 403):
            return SourceResolution("forbidden", {"url": ref.canonical_url, "kind": ref.kind, "repo_id": ref.repo_id, "number": ref.number}, "github_forbidden", "GitHub did not authorize this source", False)
        if response.status_code == 404:
            return SourceResolution("not_found", {"url": ref.canonical_url, "kind": ref.kind, "repo_id": ref.repo_id, "number": ref.number}, "github_not_found", "GitHub source was not found or is not visible", False)
        if response.status_code >= 500:
            return SourceResolution("unavailable", {"url": ref.canonical_url, "kind": ref.kind, "repo_id": ref.repo_id, "number": ref.number}, "github_unavailable", "GitHub returned a temporary server error", True)
        if response.status_code < 200 or response.status_code >= 300 or not isinstance(payload, dict):
            return SourceResolution("unavailable", {"url": ref.canonical_url, "kind": ref.kind, "repo_id": ref.repo_id, "number": ref.number}, "github_bad_response", "GitHub returned an unexpected response", True)
        try:
            source = self._normalize(ref, payload)
        except SourceResolutionError:
            raise
        return SourceResolution("resolved", source)

    @staticmethod
    def _normalize(ref: GitHubRef, payload: Mapping[str, Any]) -> dict[str, Any]:
        # Keep only metadata needed to establish an immutable source snapshot.
        # Raw API payloads can contain untrusted HTML/markup and are not copied
        # wholesale into task records.
        base = payload.get("base") if isinstance(payload.get("base"), Mapping) else {}
        head = payload.get("head") if isinstance(payload.get("head"), Mapping) else {}
        source: dict[str, Any] = {
            "url": ref.canonical_url,
            "api_url": ref.api_url,
            "kind": ref.kind,
            "repo_id": ref.repo_id,
            "number": ref.number,
            "title": str(payload.get("title") or ""),
            "body": str(payload.get("body") or ""),
            "state": str(payload.get("state") or "unknown"),
            "author": str((payload.get("user") or {}).get("login") or "") if isinstance(payload.get("user"), Mapping) else "",
            "updated_at": str(payload.get("updated_at") or ""),
            "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source_sha": str(payload.get("sha") or head.get("sha") or "") if ref.kind == "pr" else str(payload.get("id") or ""),
        }
        if ref.kind == "pr":
            base_ref = str(base.get("ref") or "")
            base_sha = str(base.get("sha") or "")
            head_ref = str(head.get("ref") or "")
            head_sha = str(head.get("sha") or payload.get("head_sha") or "")
            if not base_sha or not head_sha:
                raise SourceResolutionError(
                    "invalid_source_response",
                    "GitHub pull request metadata lacks an exact base or head commit",
                    retryable=True,
                )
            source.update({
                "base_ref": base_ref,
                "base_sha": base_sha,
                "head_ref": head_ref,
                "head_sha": head_sha,
                "merged": bool(payload.get("merged", False)),
            })
        return source


def resolve_github_source(source_url: str, *, http_client: HTTPClient | None = None, token: str | None = None, allow_anonymous: bool | None = None, allow_write: bool | None = None) -> SourceResolution:
    return GitHubSourceResolver(http_client, token=token, allow_anonymous=allow_anonymous, allow_write=allow_write).resolve(source_url)
