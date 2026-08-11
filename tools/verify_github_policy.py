"""Verify GitHub branch protection requires the candidate freshness check."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import quote

CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, text=True, capture_output=True)


def _error_text(result: subprocess.CompletedProcess[str]) -> str:
    return result.stderr.strip() or result.stdout.strip() or "GitHub API request failed"


def _required_checks(policy: object) -> list[str]:
    if not isinstance(policy, dict):
        raise ValueError("branch policy must be an object")
    required = policy.get("required_status_checks")
    if required is None:
        return []
    if not isinstance(required, dict):
        raise ValueError("required_status_checks must be an object or null")
    contexts: list[str] = []
    raw_contexts = required.get("contexts")
    if raw_contexts is not None:
        if not isinstance(raw_contexts, list) or not all(isinstance(value, str) for value in raw_contexts):
            raise ValueError("required_status_checks.contexts must be a list of strings")
        contexts.extend(raw_contexts)
    raw_checks = required.get("checks")
    if raw_checks is not None:
        if not isinstance(raw_checks, list):
            raise ValueError("required_status_checks.checks must be a list")
        if any(not isinstance(value, dict) or not isinstance(value.get("context"), str) for value in raw_checks):
            raise ValueError("required_status_checks.checks must contain context strings")
        contexts.extend(value["context"] for value in raw_checks)
    return sorted(set(contexts))


def verify_policy(
    repository: str,
    branch: str,
    required_check: str,
    *,
    runner: CommandRunner = run_command,
) -> dict[str, Any]:
    """Return a stable, read-only branch-policy report."""

    endpoint = f"repos/{repository}/branches/{quote(branch, safe='')}/protection"
    result = runner(
        (
            "gh",
            "api",
            "--method",
            "GET",
            endpoint,
            "--header",
            "Accept: application/vnd.github+json",
        )
    )
    report: dict[str, Any] = {
        "schema": 1,
        "repository": repository,
        "branch": branch,
        "requiredCheck": required_check,
        "protected": False,
        "requiredChecks": [],
        "clean": False,
        "error": None,
    }
    if result.returncode != 0:
        detail = _error_text(result)
        report["error"] = "branch is not protected" if "404" in detail or "not protected" in detail.lower() else detail
        return report
    try:
        policy = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        report["error"] = f"GitHub API returned invalid branch policy JSON: {exc}"
        return report
    try:
        checks = _required_checks(policy)
    except ValueError as exc:
        report["error"] = f"invalid GitHub branch policy: {exc}"
        return report
    report["protected"] = True
    report["requiredChecks"] = checks
    if required_check not in checks:
        report["error"] = "required candidate freshness check is not configured"
        return report
    report["clean"] = True
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="GitHub repository in owner/name form")
    parser.add_argument("--branch", required=True)
    parser.add_argument("--required-check", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", args.repository):
        parser.error("--repository must use owner/name form")
    report = verify_policy(args.repository, args.branch, args.required_check)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
