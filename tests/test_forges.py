"""Forge-neutral change-request behavior without network access."""

import json
import subprocess
from pathlib import Path

import pytest

from gitopsctr import forges as deployment_forges


def _completed(
    command: tuple[str, ...] = (),
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


class FakeRunner:
    def __init__(self, *responses: subprocess.CompletedProcess[str] | BaseException):
        self.responses = iter(responses)
        self.calls: list[tuple[tuple[str, ...], Path | None]] = []

    def __call__(self, command: tuple[str, ...], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        self.calls.append((tuple(command), cwd))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


def _spec() -> deployment_forges.ChangeRequestSpec:
    return deployment_forges.ChangeRequestSpec(
        head="promotion/prod/abc123",
        base="deploy/prod",
        title="Promote staging to prod",
        body="Promotes the reviewed staging release.",
    )


def test_configured_github_remote_finds_exact_existing_change_request(tmp_path):
    runner = FakeRunner(
        _completed(stdout="git@github.com:example-org/example-deployment.git\n"),
        _completed(stdout='[{"url":"https://github.com/example-org/example-deployment/pull/17"}]\n'),
    )

    result = deployment_forges.ensure_change_request(_spec(), runner=runner, cwd=tmp_path)

    assert result == deployment_forges.ChangeRequestResult(
        status="existing",
        url="https://github.com/example-org/example-deployment/pull/17",
    )
    assert runner.calls == [
        (("git", "remote", "get-url", "origin"), tmp_path),
        (
            (
                "gh",
                "pr",
                "list",
                "--repo",
                "example-org/example-deployment",
                "--head",
                "promotion/prod/abc123",
                "--base",
                "deploy/prod",
                "--state",
                "open",
                "--json",
                "url",
                "--limit",
                "1",
            ),
            tmp_path,
        ),
    ]


def test_github_adapter_creates_change_request_when_none_is_open(tmp_path):
    runner = FakeRunner(
        _completed(stdout="[]\n"),
        _completed(stdout="https://github.com/example-org/example-deployment/pull/18\n"),
    )

    result = deployment_forges.ensure_change_request(
        _spec(),
        remote_url="https://github.com/example-org/example-deployment.git",
        runner=runner,
        cwd=tmp_path,
    )

    assert result == deployment_forges.ChangeRequestResult(
        status="created",
        url="https://github.com/example-org/example-deployment/pull/18",
    )
    assert runner.calls[1] == (
        (
            "gh",
            "pr",
            "create",
            "--repo",
            "example-org/example-deployment",
            "--head",
            _spec().head,
            "--base",
            _spec().base,
            "--title",
            _spec().title,
            "--body",
            _spec().body,
        ),
        tmp_path,
    )


def test_concurrent_creation_reuses_change_request_found_after_create_failure():
    url = "https://github.com/example-org/example-deployment/pull/19"
    runner = FakeRunner(
        _completed(stdout="[]"),
        _completed(returncode=1, stderr="a pull request already exists"),
        _completed(stdout=json.dumps([{"url": url}])),
    )

    result = deployment_forges.ensure_change_request(
        _spec(),
        remote_url="https://github.com/example-org/example-deployment.git",
        runner=runner,
    )

    assert result == deployment_forges.ChangeRequestResult(status="existing", url=url)
    assert [call[0][2] for call in runner.calls] == ["list", "create", "list"]


def test_missing_remote_returns_exact_manual_change_request_instructions(tmp_path):
    spec = _spec()
    runner = FakeRunner(_completed(returncode=2, stderr="No such remote 'origin'"))

    result = deployment_forges.ensure_change_request(spec, runner=runner, cwd=tmp_path)

    assert isinstance(result, deployment_forges.ManualChangeRequest)
    assert result.status == "manual"
    assert result.head == spec.head
    assert result.base == spec.base
    assert result.title == spec.title
    assert result.body == spec.body
    assert result.remote_url is None
    assert "No such remote 'origin'" in result.reason
    assert "Head: promotion/prod/abc123" in result.instructions()
    assert "Base: deploy/prod" in result.instructions()


def test_unknown_forge_returns_manual_fallback_without_invoking_a_cli():
    runner = FakeRunner()

    result = deployment_forges.ensure_change_request(
        _spec(),
        remote_url="git@gitlab.com:example-org/example-deployment.git",
        runner=runner,
    )

    assert isinstance(result, deployment_forges.ManualChangeRequest)
    assert result.remote_url == "git@gitlab.com:example-org/example-deployment.git"
    assert "no change-request adapter" in result.reason
    assert runner.calls == []


def test_unavailable_or_unauthenticated_github_cli_returns_manual_fallback():
    missing_runner = FakeRunner(FileNotFoundError("gh"))
    missing = deployment_forges.ensure_change_request(
        _spec(),
        remote_url="git@github.com:example-org/example-deployment.git",
        runner=missing_runner,
    )
    assert isinstance(missing, deployment_forges.ManualChangeRequest)
    assert "gh" in missing.reason

    unauthenticated_runner = FakeRunner(
        _completed(returncode=4, stderr="To get started with GitHub CLI, run: gh auth login")
    )
    unauthenticated = deployment_forges.ensure_change_request(
        _spec(),
        remote_url="git@github.com:example-org/example-deployment.git",
        runner=unauthenticated_runner,
    )
    assert isinstance(unauthenticated, deployment_forges.ManualChangeRequest)
    assert "gh auth login" in unauthenticated.reason


@pytest.mark.parametrize(
    ("payload", "label", "status"),
    [
        ({"state": "OPEN", "labels": []}, None, "eligible"),
        ({"state": "OPEN", "labels": [{"name": "preview"}]}, "preview", "eligible"),
        ({"state": "OPEN", "labels": []}, "preview", "ineligible"),
        ({"state": "MERGED", "labels": [{"name": "preview"}]}, "preview", "ineligible"),
    ],
)
def test_github_preview_eligibility_is_fail_closed_and_label_aware(payload, label, status):
    result = deployment_forges.github_preview_eligibility(payload, label)

    assert result.status == status


def test_github_preview_adapter_reads_pull_request_state(tmp_path):
    runner = FakeRunner(_completed(stdout='{"state":"OPEN","mergedAt":null,"labels":[]}\n'))

    result = deployment_forges.preview_eligibility(
        "github:example-org/example-deployment#17",
        remote_url="git@github.com:example-org/example-deployment.git",
        runner=runner,
        cwd=tmp_path,
    )

    assert result.status == "eligible"
    assert runner.calls == [
        (
            (
                "gh",
                "pr",
                "view",
                "17",
                "--repo",
                "example-org/example-deployment",
                "--json",
                "state,mergedAt,labels",
            ),
            tmp_path,
        )
    ]


@pytest.mark.parametrize(
    ("payload", "label", "status"),
    [
        ({"state": "opened", "labels": []}, None, "eligible"),
        ({"state": "opened", "labels": ["preview"]}, "preview", "eligible"),
        ({"state": "opened", "labels": []}, "preview", "ineligible"),
        ({"state": "merged", "labels": ["preview"]}, "preview", "ineligible"),
        ({"state": "locked", "labels": []}, None, "unknown"),
    ],
)
def test_gitlab_preview_eligibility_is_fail_closed_and_label_aware(payload, label, status):
    result = deployment_forges.gitlab_preview_eligibility(payload, label)

    assert result.status == status


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        ("gitlab:group/project!17", ("group/project", 17)),
        ("https://gitlab.com/group/subgroup/project/-/merge_requests/18", ("group/subgroup/project", 18)),
        ("gitlab:group/project#17", None),
        ("https://example.com/group/project/-/merge_requests/18", None),
    ],
)
def test_gitlab_merge_request_identity_requires_canonical_form(identity, expected):
    assert deployment_forges.gitlab_merge_request_identity(identity) == expected


def test_gitlab_preview_adapter_reads_merge_request_state(tmp_path):
    runner = FakeRunner(_completed(stdout='{"state":"opened","merged_at":null,"labels":["preview"]}\n'))

    result = deployment_forges.preview_eligibility(
        "gitlab:group/subgroup/example-deployment!17",
        remote_url="git@gitlab.com:group/subgroup/example-deployment.git",
        required_label="preview",
        runner=runner,
        cwd=tmp_path,
    )

    assert result.status == "eligible"
    assert runner.calls == [
        (
            (
                "glab",
                "mr",
                "view",
                "17",
                "--repo",
                "group/subgroup/example-deployment",
                "--output",
                "json",
            ),
            tmp_path,
        )
    ]


def test_opaque_preview_identity_does_not_guess_forge_state():
    result = deployment_forges.preview_eligibility(
        "pull-123",
        remote_url="git@github.com:example-org/example-deployment.git",
        runner=FakeRunner(),
    )

    assert result.status == "unknown"


def test_explicit_adapter_is_the_plugin_seam_for_other_forges():
    expected = deployment_forges.ChangeRequestResult(
        status="created", url="https://gitlab.example/group/project/-/merge_requests/3"
    )

    class GitLabAdapter:
        def __init__(self):
            self.received = None

        def ensure_change_request(self, spec):
            self.received = spec
            return expected

    adapter = GitLabAdapter()

    result = deployment_forges.ensure_change_request(
        _spec(),
        remote_url="git@gitlab.example:group/project.git",
        adapter=adapter,
    )

    assert result == expected
    assert adapter.received == _spec()


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_github_candidate_event_requires_exact_heads(event):
    candidate = "a" * 40
    target = "b" * 40
    payload = (
        {
            "pull_request": {
                "head": {"sha": candidate},
                "base": {"sha": target},
            }
        }
        if event == "pull_request"
        else {"head_sha": candidate, "base_sha": target}
    )

    result = deployment_forges.verify_github_candidate_heads(
        payload,
        event,
        candidate_revision=candidate,
        target_revision=target,
    )

    assert result.candidate_revision == candidate
    assert result.target_revision == target


@pytest.mark.parametrize(
    ("payload", "event", "message"),
    [
        ({}, "pull_request", "missing"),
        ({"head_sha": "a" * 40, "base_sha": "b" * 40}, "pull_request", "missing"),
        ({"head_sha": "a" * 40, "base_sha": "b" * 40}, "merge_group", "does not match"),
        ({"head_sha": "a" * 40, "base_sha": "b" * 40}, "unknown", "unsupported"),
    ],
)
def test_github_candidate_event_fails_closed(payload, event, message):
    with pytest.raises(deployment_forges.OperationError, match=message):
        deployment_forges.verify_github_candidate_heads(
            payload,
            event,
            candidate_revision="c" * 40,
            target_revision="b" * 40,
        )
