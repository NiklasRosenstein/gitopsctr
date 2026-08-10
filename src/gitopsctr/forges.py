"""Create or find forge-hosted review requests for deployment changes.

The deployment controller publishes candidate refs before calling this module. This module never
updates a target ref: it only opens (or finds) the change request that reviews candidate changes.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlsplit

from gitopsctr.errors import OperationError

GitHubCandidateEventName = Literal["pull_request", "merge_group"]
PreviewEligibilityStatus = Literal["eligible", "ineligible", "unknown"]


@dataclass(frozen=True)
class GitHubCandidateHeads:
    """Head data extracted from a GitHub gated-candidate event."""

    event: GitHubCandidateEventName
    candidate_revision: str
    target_revision: str


@dataclass(frozen=True)
class PreviewEligibility:
    """Forge eligibility used by preview garbage collection."""

    status: PreviewEligibilityStatus
    reason: str


def github_preview_eligibility(payload: object, required_label: str | None = None) -> PreviewEligibility:
    """Interpret a trusted GitHub pull-request payload as an eligibility decision.

    Closed or merged requests are always ineligible. When configured, a required
    label must be present on an open request. Malformed or incomplete payloads
    remain ``unknown`` so cleanup fails closed.
    """

    if not isinstance(payload, dict):
        return PreviewEligibility("unknown", "GitHub pull-request payload is missing")
    state = payload.get("state")
    if not isinstance(state, str):
        return PreviewEligibility("unknown", "GitHub pull-request payload has no state")
    normalized_state = state.upper()
    if normalized_state in {"CLOSED", "MERGED"} or payload.get("mergedAt") is not None:
        return PreviewEligibility("ineligible", "pull request is closed or merged")
    if normalized_state != "OPEN":
        return PreviewEligibility("unknown", f"unsupported GitHub pull-request state {state!r}")
    if required_label is None:
        return PreviewEligibility("eligible", "pull request is open")
    labels = payload.get("labels")
    if not isinstance(labels, list):
        return PreviewEligibility("unknown", "GitHub pull-request payload has no labels")
    names = {item.get("name") for item in labels if isinstance(item, dict) and isinstance(item.get("name"), str)}
    if required_label not in names:
        return PreviewEligibility("ineligible", f"required label {required_label!r} is absent")
    return PreviewEligibility("eligible", f"required label {required_label!r} is present")


def github_pull_request_identity(identity: str) -> tuple[str, int] | None:
    """Parse a forge-stable GitHub pull-request identity.

    Supported forms are a canonical pull-request URL and
    ``github:owner/repository#number``. Opaque identities deliberately return
    ``None`` rather than guessing a repository or number.
    """

    value = identity.strip()
    if value.startswith("github:"):
        match = re.fullmatch(r"github:([^/]+/[^#]+)#([1-9][0-9]*)", value)
        if match:
            return match.group(1), int(match.group(2))
        return None
    parsed = urlsplit(value)
    if parsed.hostname not in {"github.com", "www.github.com"}:
        return None
    match = re.fullmatch(r"/([^/]+/[^/]+)/pull/([1-9][0-9]*)/?", parsed.path)
    if match is None:
        return None
    return match.group(1), int(match.group(2))


def _github_revision(value: object, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value):
        raise OperationError(f"GitHub event is missing a valid {field}")
    return value.lower()


def github_candidate_heads(payload: object, event: str) -> GitHubCandidateHeads:
    """Extract exact candidate and target heads from supported GitHub event payloads.

    This validates only forge-provided identity data.  It does not claim that GitHub
    branch protection or merge rules are configured; callers must still run the local
    commit-graph verifier before accepting the candidate.
    """

    if event == "pull_request":
        if not isinstance(payload, dict):
            raise OperationError("GitHub pull_request event payload is missing")
        pull_request = payload.get("pull_request")
        if not isinstance(pull_request, dict):
            raise OperationError("GitHub pull_request event is missing pull_request data")
        head = pull_request.get("head")
        base = pull_request.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise OperationError("GitHub pull_request event is missing head or base data")
        return GitHubCandidateHeads(
            event="pull_request",
            candidate_revision=_github_revision(head.get("sha"), "pull_request.head.sha"),
            target_revision=_github_revision(base.get("sha"), "pull_request.base.sha"),
        )

    if event == "merge_group":
        if not isinstance(payload, dict):
            raise OperationError("GitHub merge_group event payload is missing")
        return GitHubCandidateHeads(
            event="merge_group",
            candidate_revision=_github_revision(payload.get("head_sha"), "merge_group.head_sha"),
            target_revision=_github_revision(payload.get("base_sha"), "merge_group.base_sha"),
        )

    raise OperationError(f"unsupported GitHub gated-candidate event: {event!r}")


def verify_github_candidate_heads(
    payload: object,
    event: str,
    *,
    candidate_revision: str | None,
    target_revision: str | None,
) -> GitHubCandidateHeads:
    """Fail closed unless a GitHub event names the exact candidate and target heads."""

    heads = github_candidate_heads(payload, event)
    expected_candidate = _github_revision(candidate_revision, "expected candidate head")
    expected_target = _github_revision(target_revision, "expected target head")
    if heads.candidate_revision != expected_candidate:
        raise OperationError("GitHub event candidate head does not match the published candidate")
    if heads.target_revision != expected_target:
        raise OperationError("GitHub event target head does not match the current target head")
    return heads


@dataclass(frozen=True)
class ChangeRequestSpec:
    """All forge-neutral information needed to request a reviewed ref change."""

    head: str
    base: str
    title: str
    body: str


@dataclass(frozen=True)
class ChangeRequestResult:
    """A change request that was created now or already existed."""

    status: Literal["created", "existing"]
    url: str


@dataclass(frozen=True)
class ManualChangeRequest:
    """Exact instructions for creating a change request outside the controller."""

    reason: str
    head: str
    base: str
    title: str
    body: str
    remote_url: str | None
    status: Literal["manual"] = "manual"

    def instructions(self) -> str:
        return (
            "Create the change request manually with these exact values:\n"
            f"Head: {self.head}\n"
            f"Base: {self.base}\n"
            f"Title: {self.title}\n"
            f"Body:\n{self.body}"
        )


ChangeRequestOutcome = ChangeRequestResult | ManualChangeRequest


@dataclass(frozen=True)
class ForgeLocation:
    """A supported forge and repository parsed from a Git remote URL."""

    forge: Literal["github"]
    repository: str


class CommandRunner(Protocol):
    def __call__(self, command: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]: ...


class ChangeRequestAdapter(Protocol):
    def ensure_change_request(self, spec: ChangeRequestSpec) -> ChangeRequestOutcome: ...


class PreviewEligibilityAdapter(Protocol):
    def preview_eligibility(self, request_identity: str, required_label: str | None = None) -> PreviewEligibility: ...


def run_command(command: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run one dependency-free CLI command without raising for its exit status."""
    return subprocess.run(
        command,
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
    )


def _remote_host_and_path(remote_url: str) -> tuple[str, str] | None:
    remote_url = remote_url.strip()
    if not remote_url:
        return None

    if "://" in remote_url:
        parsed = urlsplit(remote_url)
        if not parsed.hostname:
            return None
        return parsed.hostname.lower(), parsed.path.lstrip("/")

    scp_style = re.fullmatch(r"(?:[^@/:]+@)?([^/:]+):(.+)", remote_url)
    if scp_style:
        return scp_style.group(1).lower(), scp_style.group(2).lstrip("/")
    return None


def detect_forge(remote_url: str) -> ForgeLocation | None:
    """Recognize a supported forge from a configured Git remote URL."""
    remote = _remote_host_and_path(remote_url)
    if remote is None:
        return None
    host, path = remote
    if host not in {"github.com", "www.github.com"}:
        return None

    repository_path = path.removesuffix(".git").rstrip("/")
    parts = repository_path.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return ForgeLocation(forge="github", repository="/".join(parts))


def _manual(
    spec: ChangeRequestSpec,
    reason: str,
    remote_url: str | None,
) -> ManualChangeRequest:
    return ManualChangeRequest(
        reason=reason,
        head=spec.head,
        base=spec.base,
        title=spec.title,
        body=spec.body,
        remote_url=remote_url,
    )


def _process_error(result: subprocess.CompletedProcess[str], fallback: str) -> str:
    return result.stderr.strip() or result.stdout.strip() or fallback


def _url_from_output(output: str) -> str | None:
    matches = re.findall(r"https?://[^\s]+", output)
    return matches[-1].rstrip(".,)") if matches else None


@dataclass(frozen=True)
class GitHubChangeRequestAdapter:
    """GitHub change requests implemented with ``gh`` local or token authentication."""

    repository: str
    remote_url: str
    runner: CommandRunner = run_command
    cwd: Path | None = None

    def _list_open(self, spec: ChangeRequestSpec) -> tuple[str | None, str | None]:
        result = self.runner(
            (
                "gh",
                "pr",
                "list",
                "--repo",
                self.repository,
                "--head",
                spec.head,
                "--base",
                spec.base,
                "--state",
                "open",
                "--json",
                "url",
                "--limit",
                "1",
            ),
            cwd=self.cwd,
        )
        if result.returncode != 0:
            return None, _process_error(result, "GitHub CLI could not list pull requests")
        try:
            entries = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            return None, "GitHub CLI returned invalid pull-request data"
        if not isinstance(entries, list):
            return None, "GitHub CLI returned invalid pull-request data"
        if not entries:
            return None, None
        first = entries[0]
        url = first.get("url") if isinstance(first, dict) else None
        if not isinstance(url, str) or not url.strip():
            return None, "GitHub CLI returned a pull request without a URL"
        return url.strip(), None

    def _manual(self, spec: ChangeRequestSpec, reason: str) -> ManualChangeRequest:
        return _manual(spec, reason, self.remote_url)

    def ensure_change_request(self, spec: ChangeRequestSpec) -> ChangeRequestOutcome:
        try:
            existing_url, error = self._list_open(spec)
            if error:
                return self._manual(spec, error)
            if existing_url:
                return ChangeRequestResult(status="existing", url=existing_url)

            created = self.runner(
                (
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    self.repository,
                    "--head",
                    spec.head,
                    "--base",
                    spec.base,
                    "--title",
                    spec.title,
                    "--body",
                    spec.body,
                ),
                cwd=self.cwd,
            )
            if created.returncode == 0:
                created_url = _url_from_output(created.stdout)
                if created_url:
                    return ChangeRequestResult(status="created", url=created_url)

                # A successful create is authoritative, but query once more to obtain its URL.
                created_url, list_error = self._list_open(spec)
                if created_url:
                    return ChangeRequestResult(status="created", url=created_url)
                return self._manual(
                    spec,
                    list_error or "GitHub CLI created a pull request but returned no URL",
                )

            # Another caller can win between the list and create calls. Re-list before asking
            # for manual action so deterministic retries do not create duplicate requests.
            existing_url, _list_error = self._list_open(spec)
            if existing_url:
                return ChangeRequestResult(status="existing", url=existing_url)
            return self._manual(
                spec,
                _process_error(created, "GitHub CLI could not create the pull request"),
            )
        except OSError as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            return self._manual(spec, f"GitHub CLI is unavailable: {detail}")


@dataclass(frozen=True)
class GitHubPreviewEligibilityAdapter:
    """Read pull-request eligibility without mutating forge state."""

    remote_url: str
    runner: CommandRunner = run_command
    cwd: Path | None = None

    def preview_eligibility(self, request_identity: str, required_label: str | None = None) -> PreviewEligibility:
        parsed = github_pull_request_identity(request_identity)
        if parsed is None:
            return PreviewEligibility("unknown", "request identity is not a supported GitHub pull request")
        repository, number = parsed
        result = self.runner(
            (
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                repository,
                "--json",
                "state,mergedAt,labels",
            ),
            cwd=self.cwd,
        )
        if result.returncode != 0:
            return PreviewEligibility("unknown", _process_error(result, "GitHub CLI could not inspect pull request"))
        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            return PreviewEligibility("unknown", "GitHub CLI returned invalid pull-request data")
        return github_preview_eligibility(payload, required_label)


def preview_eligibility(
    request_identity: str,
    *,
    required_label: str | None = None,
    remote_url: str | None = None,
    runner: CommandRunner = run_command,
    cwd: Path | None = None,
    adapter: PreviewEligibilityAdapter | None = None,
) -> PreviewEligibility:
    """Evaluate preview eligibility through a read-only forge adapter."""

    if adapter is not None:
        return adapter.preview_eligibility(request_identity, required_label)
    if remote_url is None:
        try:
            result = runner(("git", "remote", "get-url", "origin"), cwd=cwd)
        except OSError as exc:
            return PreviewEligibility("unknown", f"Git is unavailable: {exc}")
        if result.returncode != 0:
            return PreviewEligibility("unknown", _process_error(result, "could not read Git remote"))
        remote_url = result.stdout.strip()
    location = detect_forge(remote_url)
    if location is None or location.forge != "github":
        return PreviewEligibility("unknown", f"no preview eligibility adapter is available for remote {remote_url!r}")
    return GitHubPreviewEligibilityAdapter(remote_url, runner, cwd).preview_eligibility(
        request_identity,
        required_label,
    )


def ensure_change_request(
    spec: ChangeRequestSpec,
    *,
    remote: str = "origin",
    remote_url: str | None = None,
    adapter: ChangeRequestAdapter | None = None,
    runner: CommandRunner = run_command,
    cwd: Path | None = None,
) -> ChangeRequestOutcome:
    """Create or find the requested review, or return exact manual instructions.

    Supplying ``adapter`` bypasses built-in detection and is the plugin seam for other forges.
    Supplying ``remote_url`` bypasses the local ``git remote`` lookup for callers that already
    know which configured remote they published to.
    """
    if adapter is not None:
        return adapter.ensure_change_request(spec)

    if remote_url is None:
        try:
            result = runner(("git", "remote", "get-url", remote), cwd=cwd)
        except OSError as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            return _manual(spec, f"Git is unavailable: {detail}", None)
        if result.returncode != 0:
            return _manual(
                spec,
                _process_error(result, f"could not read Git remote {remote!r}"),
                None,
            )
        remote_url = result.stdout.strip()

    location = detect_forge(remote_url)
    if location is None:
        return _manual(
            spec,
            f"no change-request adapter is available for remote {remote_url!r}",
            remote_url,
        )
    if location.forge == "github":
        return GitHubChangeRequestAdapter(
            repository=location.repository,
            remote_url=remote_url,
            runner=runner,
            cwd=cwd,
        ).ensure_change_request(spec)

    # Literal typing makes this unreachable until another built-in adapter is added.
    return _manual(
        spec,
        f"no change-request adapter is available for remote {remote_url!r}",
        remote_url,
    )
