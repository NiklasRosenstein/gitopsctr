"""Verify the exact GitHub candidate and target heads used by a CI event."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from gitopsctr.errors import OperationError
from gitopsctr.forges import github_candidate_heads
from gitopsctr.state import GitStateStore


def verify_event(event_path: Path, event_name: str, repository: Path) -> str:
    """Verify the event head is checked out and has the required commit shape."""

    try:
        payload = json.loads(event_path.read_text())
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise OperationError(f"cannot read GitHub event payload: {exc}") from exc
    heads = github_candidate_heads(payload, event_name)
    checked_out = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=False,
        text=True,
        capture_output=True,
    )
    if checked_out.returncode != 0:
        raise OperationError("cannot resolve the checked-out GitHub candidate")
    checked_out_revision = checked_out.stdout.strip().lower()
    if checked_out_revision != heads.candidate_revision:
        raise OperationError(
            "checked-out revision does not match the GitHub event candidate head: "
            f"{checked_out_revision} != {heads.candidate_revision}"
        )
    candidate = GitStateStore(repository).verify_gated_candidate(
        heads.candidate_revision,
        heads.target_revision,
    )
    return f"verified candidate {candidate.revision} against target {candidate.target_revision}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", type=Path, required=True, help="GitHub event JSON path")
    parser.add_argument("--event-name", required=True, choices=("pull_request", "merge_group"))
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        print(verify_event(args.event, args.event_name, args.repository.resolve()))
    except OperationError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
