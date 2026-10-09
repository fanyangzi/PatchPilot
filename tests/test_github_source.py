from __future__ import annotations

import json

import pytest

from patchpilot.integrations.github_source import (
    GitHubSourceResolver,
    HTTPResponse,
    SourceResolutionError,
    parse_github_url,
)


class FakeHTTP:
    def __init__(self, status=200, payload=None, url="https://api.github.com/repos/acme/widget/pulls/7"):
        self.status = status
        self.payload = payload or {}
        self.url = url
        self.calls = []

    def get(self, url, *, headers, timeout):
        self.calls.append((url, dict(headers), timeout))
        return HTTPResponse(self.status, {"content-type": "application/json"}, json.dumps(self.payload).encode(), self.url)


def test_parse_github_pr_and_issue_canonicalizes_refs():
    pr = parse_github_url("https://github.com/acme/widget/pull/7")
    assert (pr.owner, pr.repo, pr.kind, pr.number) == ("acme", "widget", "pr", 7)
    assert pr.api_url == "https://api.github.com/repos/acme/widget/pulls/7"
    issue = parse_github_url("https://github.com/acme/widget/issues/12/")
    assert issue.kind == "issue"
    assert issue.canonical_url.endswith("/issues/12")


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/acme/widget/pull/1",
        "https://github.com.evil.example/acme/widget/pull/1",
        "https://user:secret@github.com/acme/widget/pull/1",
        "https://github.com:443/acme/widget/pull/1",
        "https://github.com:notaport/acme/widget/pull/1",
        "https://github.com/acme/widget/pull/1?redirect=http://127.0.0.1",
        "https://127.0.0.1/acme/widget/pull/1",
        "https://github.com/acme/widget/commit/1",
    ],
)
def test_parse_rejects_unsafe_or_ambiguous_urls(url):
    with pytest.raises(SourceResolutionError) as exc:
        parse_github_url(url)
    assert exc.value.code in {"invalid_source_url", "ssrf_blocked"}


def test_missing_token_is_explicit_pending_and_never_calls_transport(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    http = FakeHTTP()
    result = GitHubSourceResolver(http, allow_anonymous=False).resolve("https://github.com/acme/widget/pull/7")
    assert result.status == "pending"
    assert result.code == "github_auth_not_configured"
    assert result.retryable is True
    assert http.calls == []
    assert "Authorization" not in result.to_dict().__repr__()


def test_pr_resolution_is_read_only_and_redacts_auth_from_snapshot():
    http = FakeHTTP(payload={
        "number": 7,
        "title": "Fix parser",
        "body": "User supplied issue text",
        "state": "open",
        "user": {"login": "maintainer"},
        "updated_at": "2026-10-09T10:00:00Z",
        "base": {"ref": "main", "sha": "base-sha"},
        "head": {"ref": "fix/parser", "sha": "head-sha"},
        "merged": False,
    })
    result = GitHubSourceResolver(http, token="super-secret").resolve("https://github.com/acme/widget/pull/7")
    assert result.status == "resolved"
    assert result.source["base_sha"] == "base-sha"
    assert result.source["head_sha"] == "head-sha"
    assert result.source["repo_id"] == "acme/widget"
    assert result.source["url"] == "https://github.com/acme/widget/pull/7"
    assert "super-secret" not in json.dumps(result.to_dict())
    assert http.calls[0][0] == "https://api.github.com/repos/acme/widget/pulls/7"
    assert http.calls[0][1]["Authorization"] == "Bearer super-secret"


def test_permission_and_not_found_are_explicit_non_success_states():
    for status, expected in [(403, "forbidden"), (404, "not_found"), (503, "unavailable")]:
        http = FakeHTTP(status=status, payload={})
        result = GitHubSourceResolver(http, token="test").resolve("https://github.com/acme/widget/issues/7")
        assert result.status == expected
        assert result.source["repo_id"] == "acme/widget"


def test_pr_without_exact_base_or_head_is_not_marked_resolved():
    http = FakeHTTP(payload={"title": "incomplete", "base": {"ref": "main"}, "head": {"ref": "fix"}})
    with pytest.raises(SourceResolutionError) as exc:
        GitHubSourceResolver(http, token="test").resolve("https://github.com/acme/widget/pull/7")
    assert exc.value.code == "invalid_source_response"
    assert exc.value.retryable is True
