"""Offline tests for the read-only GitHub branch-policy verifier."""

from __future__ import annotations

import json
import subprocess

from tools.verify_github_policy import verify_policy


def _runner(payload: object, returncode: int = 0, stderr: str | None = None):
    def run(_command):
        return subprocess.CompletedProcess(
            args=("gh", "api"),
            returncode=returncode,
            stdout=json.dumps(payload) if returncode == 0 else "",
            stderr=stderr if stderr is not None else ("Branch not protected (HTTP 404)" if returncode else ""),
        )

    return run


def test_policy_accepts_contexts_and_checks_shapes():
    report = verify_policy(
        "example/project",
        "main",
        "CI / Verify gated candidate freshness",
        runner=_runner(
            {
                "required_status_checks": {
                    "contexts": ["lint"],
                    "checks": [{"context": "CI / Verify gated candidate freshness", "app_id": 1}],
                }
            }
        ),
    )

    assert report["clean"] is True
    assert report["protected"] is True
    assert report["requiredChecks"] == ["CI / Verify gated candidate freshness", "lint"]


def test_policy_rejects_unprotected_branch():
    report = verify_policy(
        "example/project",
        "main",
        "CI / Verify gated candidate freshness",
        runner=_runner({}, returncode=1),
    )

    assert report["clean"] is False
    assert report["protected"] is False
    assert report["error"] == "branch is not protected"


def test_policy_rejects_missing_required_check():
    report = verify_policy(
        "example/project",
        "main",
        "CI / Verify gated candidate freshness",
        runner=_runner({"required_status_checks": {"contexts": ["lint"]}}),
    )

    assert report["clean"] is False
    assert report["protected"] is True
    assert report["error"] == "required candidate freshness check is not configured"


def test_policy_encodes_branch_names_and_uses_read_only_get():
    commands = []

    def runner(command):
        commands.append(command)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps(
                {
                    "required_status_checks": {
                        "contexts": ["CI / Verify gated candidate freshness"],
                    }
                }
            ),
            stderr="",
        )

    report = verify_policy(
        "example/project",
        "release/preview",
        "CI / Verify gated candidate freshness",
        runner=runner,
    )

    assert report["clean"] is True
    assert commands == [
        (
            "gh",
            "api",
            "--method",
            "GET",
            "repos/example/project/branches/release%2Fpreview/protection",
            "--header",
            "Accept: application/vnd.github+json",
        )
    ]


def test_policy_rejects_malformed_status_checks():
    report = verify_policy(
        "example/project",
        "main",
        "CI / Verify gated candidate freshness",
        runner=_runner({"required_status_checks": {"contexts": ["lint", 7]}}),
    )

    assert report["clean"] is False
    assert report["protected"] is False
    assert report["error"] == (
        "invalid GitHub branch policy: required_status_checks.contexts must be a list of strings"
    )
