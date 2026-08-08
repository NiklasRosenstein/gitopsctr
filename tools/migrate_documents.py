#!/usr/bin/env python3
"""One-shot migration from the original JSON documents to YAML resources.

The script operates on local branches, creates ordinary forward commits, and
never rewrites existing history.  Run it from a clean checkout of the source
branch; pass ``--apply`` to update refs (the default is a preview).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from gitopsctr import cli
from gitopsctr.formats import DocumentFormat, document_candidates, load_document, write_document

ROOT = Path.cwd().resolve()


def git(*args: str, check: bool = True, input_text: str | None = None, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        input=input_text,
        text=True,
        capture_output=True,
        check=check,
        env=env,
    )
    return result.stdout.strip()


def commit_tree(directory: Path, parent: str, message: str) -> str:
    with tempfile.TemporaryDirectory(prefix="gitopsctr-migration-index-") as temporary:
        index = str(Path(temporary) / "index")
        environment = os.environ | {"GIT_INDEX_FILE": index}
        git("read-tree", "--empty", env=environment)
        for path in sorted(path for path in directory.rglob("*") if path.is_file()):
            if path.is_symlink():
                raise RuntimeError(f"migration tree contains a symbolic link: {path}")
            blob = git("hash-object", "-w", str(path))
            relative = path.relative_to(directory).as_posix()
            git("update-index", "--add", "--cacheinfo", f"100644,{blob},{relative}", env=environment)
        tree = git("write-tree", env=environment)
    identity = os.environ | {
        "GIT_AUTHOR_NAME": os.environ.get("GITOPSCTR_GIT_AUTHOR_NAME", "gitopsctr"),
        "GIT_AUTHOR_EMAIL": os.environ.get("GITOPSCTR_GIT_AUTHOR_EMAIL", "gitopsctr@users.noreply.github.com"),
        "GIT_COMMITTER_NAME": os.environ.get("GITOPSCTR_GIT_AUTHOR_NAME", "gitopsctr"),
        "GIT_COMMITTER_EMAIL": os.environ.get("GITOPSCTR_GIT_AUTHOR_EMAIL", "gitopsctr@users.noreply.github.com"),
    }
    return git("commit-tree", tree, "-p", parent, input_text=f"{message}\n", env=identity)


def materialize(revision: str, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "archive", "--format=tar", revision], cwd=ROOT, check=True, stdout=subprocess.PIPE).stdout
    import tarfile

    with tempfile.TemporaryFile() as stream:
        stream.write(archive)
        stream.seek(0)
        with tarfile.open(fileobj=stream, mode="r:") as tar:
            tar.extractall(directory, filter="data")


def remove_document(path: Path) -> None:
    for candidate in document_candidates(path.parent, path.stem):
        candidate.unlink()


def write_yaml(path: Path, document: dict[str, Any]) -> None:
    remove_document(path)
    write_document(path.with_suffix(".yaml"), document, format=DocumentFormat.YAML)


def convert_environment(tree: Path, environment_name: str) -> None:
    root = tree / "deployment" / "environments" / environment_name
    paths = document_candidates(root, "environment")
    if len(paths) != 1:
        raise RuntimeError(f"expected one environment document for {environment_name}")
    environment = cli.normalize_environment_document(load_document(paths[0]), environment_name)
    write_yaml(root / "environment.yaml", cli.serialize_environment_document(environment))
    units = root / "units"
    for path in sorted(path for path in units.glob("*") if path.suffix in {".json", ".yaml", ".yml"}):
        unit = cli.normalize_unit_document(load_document(path), path.stem)
        write_yaml(units / f"{path.stem}.yaml", cli.serialize_unit_document(unit, profile="authored"))


def convert_desired(tree: Path, source_revision: str) -> None:
    units = tree / "units"
    for path in sorted(path for path in units.glob("*") if path.suffix in {".json", ".yaml", ".yml"}):
        unit = cli.normalize_unit_document(load_document(path), path.stem)
        source = unit.get("source")
        if isinstance(source, dict) and isinstance(source.get("revision"), str):
            unit = {**unit, "source": {**source, "revision": source_revision}}
        write_yaml(units / f"{path.stem}.yaml", cli.serialize_unit_document(unit, profile="desired"))
    promotion_paths = document_candidates(tree, "promotion")
    if promotion_paths:
        if len(promotion_paths) > 1:
            raise RuntimeError("multiple promotion document formats exist")
        promotion = cli.normalize_promotion_document(load_document(promotion_paths[0]))
        promotion = {**promotion, "specificationRevision": source_revision}
        write_yaml(tree / "promotion.yaml", cli.serialize_promotion_document(promotion))


def convert_observed(tree: Path, desired_revision: str, desired_tree: Path) -> None:
    units = tree / "units"
    for path in sorted(path for path in units.glob("*") if path.suffix in {".json", ".yaml", ".yml"}):
        receipt = cli.normalize_receipt_document(load_document(path), path.stem)
        desired_path = cli.unit_document_path(desired_tree, path.stem)
        desired_blob = git("hash-object", str(desired_path))
        receipt = {
            **receipt,
            "desired": {**receipt.get("desired", {}), "revision": desired_revision, "unitBlob": desired_blob},
        }
        write_yaml(units / f"{path.stem}.yaml", cli.serialize_receipt_document(receipt))


def local_refs(prefix: str) -> list[tuple[str, str]]:
    output = git("for-each-ref", "--format=%(refname) %(objectname)", f"refs/heads/{prefix}")
    refs: list[tuple[str, str]] = []
    for line in output.splitlines():
        parts = line.split()
        if line:
            if len(parts) != 2:
                raise RuntimeError(f"unexpected ref listing: {line}")
            refs.append((parts[0], parts[1]))
    return refs


def update_ref(ref: str, new: str, old: str) -> None:
    git("update-ref", ref, new, old)


def migrate(*, apply: bool, push: bool) -> dict[str, str]:
    branch = git("branch", "--show-current")
    if not branch:
        raise RuntimeError("migration requires a checked-out source branch")
    if git("status", "--porcelain"):
        raise RuntimeError("migration requires a clean working tree")

    source_ref = f"refs/heads/{branch}"
    old_source = git("rev-parse", source_ref)
    results: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="gitopsctr-migration-") as temporary:
        root = Path(temporary)
        source_tree = root / "source"
        materialize(old_source, source_tree)
        for environment_root in sorted((source_tree / "deployment" / "environments").glob("*")):
            if environment_root.is_dir():
                convert_environment(source_tree, environment_root.name)
        (source_tree / "gitopsctr.yaml").write_text("writeFormat: yaml\n")
        new_source = commit_tree(source_tree, old_source, "Migrate deployment documents to YAML resources")
        results[branch] = new_source

        desired_heads: dict[str, tuple[str, str]] = {}
        for ref, old in local_refs("deploy/"):
            environment = ref.removeprefix("refs/heads/deploy/")
            desired_tree = root / f"desired-{environment}"
            materialize(old, desired_tree)
            convert_desired(desired_tree, new_source)
            (desired_tree / "gitopsctr.yaml").write_text("writeFormat: yaml\n")
            new = commit_tree(desired_tree, old, f"Migrate desired {environment} documents to YAML resources")
            desired_heads[environment] = (old, new)
            results[ref] = new

        for ref, old in local_refs("observed/"):
            environment = ref.removeprefix("refs/heads/observed/")
            desired = desired_heads.get(environment)
            if desired is None:
                raise RuntimeError(f"observed/{environment} has no matching deploy/{environment} ref")
            observed_tree = root / f"observed-{environment}"
            desired_tree = root / f"desired-{environment}"
            materialize(old, observed_tree)
            convert_observed(observed_tree, desired[1], desired_tree)
            (observed_tree / "gitopsctr.yaml").write_text("writeFormat: yaml\n")
            new = commit_tree(observed_tree, old, f"Migrate observed {environment} receipts to YAML resources")
            results[ref] = new

    if apply:
        update_ref(source_ref, results[branch], old_source)
        for ref, old in (*[(f"refs/heads/deploy/{env}", old) for env, (old, _new) in desired_heads.items()],):
            update_ref(ref, results[ref], old)
        for ref, old in local_refs("observed/"):
            update_ref(ref, results[ref], old)
        if push:
            remote = git("remote").splitlines()[0] if git("remote") else None
            if remote:
                git("push", "--atomic", remote, *[f"{ref}:{ref}" for ref in results])
    return results


ROOT = Path(git("rev-parse", "--show-toplevel")).resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="update local refs with migration commits")
    parser.add_argument("--push", action="store_true", help="push migrated refs atomically after --apply")
    args = parser.parse_args()
    results = migrate(apply=args.apply, push=args.push)
    print(json.dumps(results, indent=2, sort_keys=True))
    if not args.apply:
        print("Preview only. Re-run with --apply to update refs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
