"""Read-only GitHub commit history MCP tools."""

from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

GITHUB_API_TIMEOUT_SECONDS = 8
GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
MAX_COMMIT_LIST_LIMIT = 30
MAX_COMMIT_FILES = 50
MAX_PATCH_CHARS_PER_FILE = 8_000
MAX_TOTAL_PATCH_CHARS = 40_000
MAX_GITHUB_RESPONSE_BYTES = 4_000_000
class GitHubReadError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _github_repository() -> tuple[str, str]:
    owner = os.getenv("GITHUB_REPO_OWNER", "").strip()
    name = os.getenv("GITHUB_REPO_NAME", "").strip()
    if not owner or not name:
        raise GitHubReadError(
            "github_configuration_missing",
            "Set both GITHUB_REPO_OWNER and GITHUB_REPO_NAME to configure the repository.",
        )
    if any(char in owner for char in "/\\") or any(char in name for char in "/\\"):
        raise GitHubReadError(
            "github_configuration_invalid",
            "GitHub owner and repository name must each be a single path segment.",
        )
    return owner, name


def _github_get(
    endpoint: str, params: dict[str, str | int] | None = None
) -> tuple[dict[str, Any] | list[Any], bool]:
    owner, name = _github_repository()
    api_path = (
        f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/"
        f"{endpoint.lstrip('/')}"
    )
    query = f"?{urlencode(params)}" if params else ""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Kube-Investigator-MCP",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = Request(f"{GITHUB_API_BASE}{api_path}{query}", headers=headers)
    try:
        with urlopen(request, timeout=GITHUB_API_TIMEOUT_SECONDS) as response:
            raw_body = response.read(MAX_GITHUB_RESPONSE_BYTES + 1)
            if len(raw_body) > MAX_GITHUB_RESPONSE_BYTES:
                raise GitHubReadError(
                    "github_response_too_large",
                    "GitHub API response exceeded the configured size limit.",
                )
            result = json.loads(raw_body)
            link_header = response.headers.get("Link", "")
    except GitHubReadError:
        raise
    except HTTPError as exc:
        response_headers = exc.headers or {}
        response_message = exc.read(4096).decode("utf-8", errors="replace").lower()
        rate_limited = (
            exc.code == 429
            or response_headers.get("X-RateLimit-Remaining") == "0"
            or bool(response_headers.get("Retry-After"))
            or "rate limit" in response_message
        )
        if rate_limited:
            raise GitHubReadError(
                "github_rate_limited",
                "GitHub rate limit reached; retry after the rate limit resets.",
            ) from exc
        if exc.code == 401:
            raise GitHubReadError(
                "github_authentication_failed",
                "GitHub rejected the configured credentials.",
            ) from exc
        if exc.code == 403:
            raise GitHubReadError(
                "github_access_denied",
                "GitHub denied access to the configured repository or commit.",
            ) from exc
        if exc.code == 404:
            raise GitHubReadError(
                "github_resource_not_found_or_inaccessible",
                "The configured repository or requested commit was not found or is not accessible.",
            ) from exc
        raise GitHubReadError(
            "github_api_error",
            f"GitHub API returned HTTP {exc.code}.",
        ) from exc
    except (TimeoutError, URLError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            raise GitHubReadError(
                "github_timeout",
                f"GitHub API request exceeded {GITHUB_API_TIMEOUT_SECONDS} seconds.",
            ) from exc
        raise GitHubReadError(
            "github_unavailable",
            "Could not connect to the GitHub API.",
        ) from exc
    except (OSError, json.JSONDecodeError):
        raise GitHubReadError(
            "github_api_error",
            "GitHub API returned an unreadable response.",
        ) from None

    if not isinstance(result, (dict, list)):
        raise GitHubReadError(
            "github_api_error",
            "GitHub API returned an unexpected response format.",
        )
    return result, 'rel="next"' in link_header


def _github_error_result(error: GitHubReadError) -> dict[str, Any]:
    return {"error": {"code": error.code, "message": error.message}}


def _commit_timestamp(commit: dict[str, Any]) -> str | None:
    details = commit.get("commit", {})
    return (
        details.get("committer", {}).get("date")
        or details.get("author", {}).get("date")
    )


def _commit_summary(commit: dict[str, Any]) -> dict[str, Any]:
    details = commit.get("commit", {})
    return {
        "sha": commit.get("sha"),
        "message": details.get("message", ""),
        "commitTimestamp": _commit_timestamp(commit),
        "url": commit.get("html_url"),
    }



def list_repo_commits(
    path: str | None = None, ref: str | None = None, limit: int = 10
) -> dict[str, Any]:
    """List recent commits in the configured GitHub repository, optionally for a file path."""
    if type(limit) is not int or limit < 1:
        return {
            "error": {
                "code": "invalid_limit",
                "message": "limit must be a positive integer; values above 30 are capped at 30.",
            }
        }
    limit = min(limit, MAX_COMMIT_LIST_LIMIT)

    params: dict[str, str | int] = {"per_page": limit}
    if path is not None:
        if (
            not isinstance(path, str)
            or not path.strip()
            or len(path) > 1_024
            or path.startswith("/")
            or "\\" in path
            or any(part in {".", ".."} for part in path.split("/"))
        ):
            return {
                "error": {
                    "code": "invalid_path",
                    "message": "path must be a repository-relative file path.",
                }
            }
        params["path"] = path
    if ref is not None:
        if not isinstance(ref, str) or not ref.strip() or len(ref) > 255:
            return {
                "error": {
                    "code": "invalid_ref",
                    "message": "ref must be a non-empty branch, tag, or commit reference.",
                }
            }
        params["sha"] = ref.strip()

    try:
        owner, name = _github_repository()
        commits, _ = _github_get("commits", params)
        if not isinstance(commits, list):
            raise GitHubReadError(
                "github_api_error",
                "GitHub API returned an unexpected commit list format.",
            )
        return {
            "repository": f"{owner}/{name}",
            "commits": [_commit_summary(commit) for commit in commits[:limit]],
            "limit": limit,
        }
    except GitHubReadError as exc:
        return _github_error_result(exc)



def get_commit(sha: str) -> dict[str, Any]:
    """Fetch a commit and a bounded summary of its changed files from the configured repository."""
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha):
        return {
            "error": {
                "code": "invalid_sha",
                "message": "sha must be a hexadecimal commit SHA between 7 and 64 characters.",
            }
        }

    try:
        owner, name = _github_repository()
        commit, has_more_files = _github_get(
            f"commits/{quote(sha, safe='')}", {"per_page": MAX_COMMIT_FILES}
        )
        if not isinstance(commit, dict):
            raise GitHubReadError(
                "github_api_error",
                "GitHub API returned an unexpected commit format.",
            )

        source_files = commit.get("files", [])
        if not isinstance(source_files, list):
            raise GitHubReadError(
                "github_api_error",
                "GitHub API returned an unexpected changed-files format.",
            )

        output_files: list[dict[str, Any]] = []
        total_patch_chars = 0
        patches_truncated = False
        for changed_file in source_files[:MAX_COMMIT_FILES]:
            patch = changed_file.get("patch")
            file_result: dict[str, Any] = {
                "filename": changed_file.get("filename"),
                "status": changed_file.get("status"),
                "additions": changed_file.get("additions", 0),
                "deletions": changed_file.get("deletions", 0),
                "patch": None,
                "patchUnavailable": not isinstance(patch, str),
                "patchTruncated": False,
            }
            if isinstance(patch, str):
                remaining = max(0, MAX_TOTAL_PATCH_CHARS - total_patch_chars)
                allowed_chars = min(MAX_PATCH_CHARS_PER_FILE, remaining)
                bounded_patch = patch[:allowed_chars]
                file_result["patch"] = bounded_patch
                total_patch_chars += len(bounded_patch)
                if len(bounded_patch) < len(patch):
                    file_result["patchTruncated"] = True
                    patches_truncated = True
            output_files.append(file_result)

        files_truncated = len(source_files) > MAX_COMMIT_FILES or has_more_files
        return {
            "repository": f"{owner}/{name}",
            "sha": commit.get("sha"),
            "message": commit.get("commit", {}).get("message", ""),
            "commitTimestamp": _commit_timestamp(commit),
            "url": commit.get("html_url"),
            "files": output_files,
            "filesTruncated": files_truncated,
            "patchesTruncated": patches_truncated,
            "truncated": files_truncated or patches_truncated,
            "patchCharacterLimit": MAX_TOTAL_PATCH_CHARS,
        }
    except GitHubReadError as exc:
        return _github_error_result(exc)



def register(mcp: Any) -> None:
    """Register GitHub tools on the shared FastMCP application."""
    mcp.tool()(list_repo_commits)
    mcp.tool()(get_commit)