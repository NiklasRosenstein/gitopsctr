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
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from gitopsctr import cli
from gitopsctr.formats import (
    PROJECT_CONFIG_NAMES,
    DocumentFormat,
    document_candidates,
    load_document,
    load_project_config,
    write_document,
)

ROOT = Path.cwd().resolve()


@dataclass(frozen=True)
class EnvironmentMigrationRefs:
    environment: str
    desired: str
    observed: str


def git(*args: str, check: bool = True, input_text: str | None = None, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "no error output"
        command = shlex.join(["git", *args])
        raise RuntimeError(f"{command} failed with exit status {result.returncode}:\n{detail}")
    return result.stdout.strip()


def git_is_ancestor(ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode not in {0, 1}:
        detail = result.stderr.strip() or result.stdout.strip() or "no error output"
        raise RuntimeError(f"git merge-base failed with exit status {result.returncode}:\n{detail}")
    return result.returncode == 0


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
    archive = subprocess.run(
        ["git", "archive", "--format=tar", revision], cwd=ROOT, check=True, stdout=subprocess.PIPE
    ).stdout
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


def write_project(tree: Path, project_name: str) -> None:
    configured = [tree / filename for filename in PROJECT_CONFIG_NAMES if (tree / filename).is_file()]
    if configured:
        if len(configured) > 1:
            raise RuntimeError("multiple Project configuration files exist")
        try:
            project = load_project_config(tree)
        except Exception as exc:
            raise RuntimeError(f"existing Project configuration is invalid: {configured[0]}") from exc
        if project.name != project_name:
            raise RuntimeError(f"existing Project configuration names project {project.name!r}, not {project_name!r}")
        return
    for filename in PROJECT_CONFIG_NAMES:
        path = tree / filename
        if path.exists():
            path.unlink()
    write_document(
        tree / "gitopsctr.yaml",
        {
            "$schema": "https://niklasrosenstein.github.io/gitopsctr/schemas/apis/gitopsctr.io/v1/Project.schema.json",
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Project",
            "metadata": {"name": project_name},
            "spec": {
                "writeFormat": "yaml",
                "environmentsPath": "deployment/environments",
                "environmentDefaults": {
                    "refs": {
                        "desired": "deploy/{environment}",
                        "observed": "observed/{environment}",
                        "candidate": "gitopsctr/candidates/{environment}/{id}",
                    }
                },
            },
        },
        format=DocumentFormat.YAML,
    )
    load_project_config(tree)


def convert_environment(tree: Path, environment_name: str) -> None:
    project = load_project_config(tree)
    root = tree.joinpath(*project.environments_path.parts, environment_name)
    paths = document_candidates(root, "environment")
    if len(paths) != 1:
        raise RuntimeError(f"expected one environment document for {environment_name}")
    environment = cli.normalize_environment_document(load_document(paths[0]), environment_name)
    write_yaml(root / "environment.yaml", cli.serialize_environment_document(environment))
    units = root / "units"
    for path in sorted(path for path in units.glob("*") if path.suffix in {".json", ".yaml", ".yml"}):
        unit = cli.parse_authored_unit_document(load_document(path), path.stem)
        write_yaml(units / f"{path.stem}.yaml", cli.serialize_unit_document(unit, profile="authored"))


def convert_desired(tree: Path, source_revision: str) -> None:
    units = tree / "units"
    for path in sorted(path for path in units.glob("*") if path.suffix in {".json", ".yaml", ".yml"}):
        unit = cli.parse_desired_unit_document(load_document(path), path.stem)
        source = getattr(unit.spec, "source", None)
        if source is not None:
            unit = unit.with_spec(replace(unit.spec, source=replace(source, revision=source_revision)))
            source = getattr(unit.spec, "source", None)
        if unit.is_legacy_compatibility:
            unit = unit.with_metadata(cli.source_tracked_metadata_for_resource(unit, source, source_revision))
        write_yaml(units / f"{path.stem}.yaml", cli.serialize_unit_document(unit, profile="desired"))
    promotion_paths = document_candidates(tree, "promotion")
    if promotion_paths:
        if len(promotion_paths) > 1:
            raise RuntimeError("multiple promotion document formats exist")
        promotion = cli.normalize_promotion_document(load_document(promotion_paths[0]))
        promotion = {**promotion, "specificationRevision": source_revision}
        write_yaml(tree / "promotion.yaml", cli.serialize_promotion_document(promotion))


def rewrite_promotion_lineage(
    tree: Path,
    desired_heads: dict[str, tuple[str, str]],
    observed_heads: dict[str, tuple[str, str]],
    desired_refs: dict[str, str],
) -> None:
    paths = document_candidates(tree, "promotion")
    if not paths:
        return
    promotion = cli.normalize_promotion_document(load_document(paths[0]))
    source = promotion.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("environment"), str):
        raise RuntimeError("promotion source is missing its environment")
    source_environment = source["environment"]
    desired = desired_heads.get(source_environment)
    observed = observed_heads.get(source_environment)
    if desired is None:
        ref = desired_refs.get(source_environment, source_environment)
        raise RuntimeError(f"promotion source {ref} has not been migrated")
    updated_source = {**source, "desiredRevision": desired[1]}
    updated_source["observedRevision"] = observed[1] if observed is not None else None
    write_yaml(
        tree / "promotion.yaml",
        cli.serialize_promotion_document({**promotion, "source": updated_source}),
    )


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


def local_ref_revision(ref: str) -> str | None:
    full_ref = ref if ref.startswith("refs/") else f"refs/heads/{ref}"
    revision = git("rev-parse", "--verify", "--quiet", full_ref, check=False)
    return revision or None


def heads_ref(ref: str) -> str:
    return ref if ref.startswith("refs/heads/") else f"refs/heads/{ref}"


def environment_names(tree: Path) -> list[str]:
    project = load_project_config(tree)
    root = tree.joinpath(*project.environments_path.parts)
    return sorted(path.name for path in root.glob("*") if path.is_dir())


def migration_ref_inventory(tree: Path) -> tuple[EnvironmentMigrationRefs, ...]:
    """Resolve every environment ref from the migrated Project configuration.

    A legacy tree without a Project is given an explicit generated Project by
    ``write_project`` before this function runs.  That generated configuration
    records the historical deploy/observed mapping instead of hiding it in
    ref discovery.
    """

    inventory: list[EnvironmentMigrationRefs] = []
    seen: dict[str, str] = {}
    for environment in environment_names(tree):
        desired, observed = cli.deployment_refs(tree, environment)
        for ref, role in ((desired, "desired"), (observed, "observed")):
            owner = seen.get(ref)
            if owner is not None:
                raise RuntimeError(f"{ref!r} is configured for multiple environment refs ({owner}, {environment})")
            seen[ref] = f"{environment} {role}"
        inventory.append(EnvironmentMigrationRefs(environment, desired, observed))
    return tuple(inventory)


def update_ref(ref: str, new: str, old: str) -> None:
    git("update-ref", ref, new, old)


def validate_remote_refs(remote: str, refs: list[str]) -> None:
    """Fetch and ensure migrating each existing ref would remain a fast-forward push."""
    git("fetch", "--prune", remote)
    stale: list[str] = []
    for ref in refs:
        short = ref.removeprefix("refs/heads/")
        remote_ref = f"refs/remotes/{remote}/{short}"
        remote_revision = git("rev-parse", "--verify", "--quiet", remote_ref, check=False)
        if not remote_revision:
            continue
        local_revision = git("rev-parse", ref)
        if not git_is_ancestor(remote_revision, local_revision):
            behind, ahead = git("rev-list", "--left-right", "--count", f"{remote_ref}...{ref}").split()
            stale.append(f"{short} (remote-only commits: {behind}, local-only commits: {ahead})")
    if stale:
        details = "\n".join(f"  - {item}" for item in stale)
        raise RuntimeError(
            "migration requires local refs that can fast-forward their remote counterparts; "
            "synchronize these refs and retry:\n" + details
        )


def migrate(*, project_name: str, apply: bool, push: bool) -> dict[str, str]:
    branch = git("branch", "--show-current")
    if not branch:
        raise RuntimeError("migration requires a checked-out source branch")
    if git("status", "--porcelain"):
        raise RuntimeError("migration requires a clean working tree")

    if push and not apply:
        raise RuntimeError("--push requires --apply")

    source_ref = f"refs/heads/{branch}"
    remote: str | None = None
    old_source = git("rev-parse", source_ref)
    results: dict[str, str] = {}
    old_refs: dict[str, str] = {source_ref: old_source}
    with tempfile.TemporaryDirectory(prefix="gitopsctr-migration-") as temporary:
        root = Path(temporary)
        source_tree = root / "source"
        materialize(old_source, source_tree)
        write_project(source_tree, project_name)
        for environment in environment_names(source_tree):
            convert_environment(source_tree, environment)
        new_source = commit_tree(source_tree, old_source, "Migrate deployment documents to YAML resources")
        results[branch] = new_source
        refs = migration_ref_inventory(source_tree)

        if push:
            remotes = git("remote").splitlines()
            if not remotes:
                raise RuntimeError("--push requires a Git remote")
            remote = remotes[0]
            configured_refs = {
                heads_ref(ref)
                for item in refs
                for ref in (item.desired, item.observed)
                if local_ref_revision(ref) is not None
            }
            validate_remote_refs(remote, [source_ref, *sorted(configured_refs)])

        desired_heads: dict[str, tuple[str, str]] = {}
        desired_trees: dict[str, Path] = {}
        desired_ref_by_environment = {item.environment: item.desired for item in refs}
        for item in refs:
            old = local_ref_revision(item.desired)
            if old is None:
                continue
            desired_ref = heads_ref(item.desired)
            if desired_ref == source_ref:
                raise RuntimeError(f"source branch {branch!r} is also the desired ref for {item.environment}")
            environment = item.environment
            desired_tree = root / f"desired-{environment}"
            materialize(old, desired_tree)
            convert_desired(desired_tree, new_source)
            new = commit_tree(desired_tree, old, f"Migrate desired {environment} documents to YAML resources")
            desired_heads[environment] = (old, new)
            desired_trees[environment] = desired_tree
            results[desired_ref] = new
            old_refs[desired_ref] = old

        # Rewrite promotion references after every desired head is known. This
        # keeps source desired revisions from pointing at pre-migration commits.
        for environment, (old, current) in list(desired_heads.items()):
            tree = desired_trees[environment]
            if not document_candidates(tree, "promotion"):
                continue
            rewrite_promotion_lineage(tree, desired_heads, {}, desired_ref_by_environment)
            new = commit_tree(tree, current, f"Migrate {environment} promotion lineage")
            desired_heads[environment] = (old, new)
            desired_ref = heads_ref(desired_ref_by_environment[environment])
            results[desired_ref] = new

        observed_heads: dict[str, tuple[str, str]] = {}
        observed_trees: dict[str, Path] = {}
        for item in refs:
            old = local_ref_revision(item.observed)
            if old is None:
                continue
            observed_ref = heads_ref(item.observed)
            environment = item.environment
            desired = desired_heads.get(environment)
            if desired is None:
                raise RuntimeError(f"{item.observed} has no matching desired ref {item.desired}")
            observed_tree = root / f"observed-{environment}"
            desired_tree = root / f"desired-{environment}"
            materialize(old, observed_tree)
            convert_observed(observed_tree, desired[1], desired_tree)
            new = commit_tree(observed_tree, old, f"Migrate observed {environment} receipts to YAML resources")
            observed_heads[environment] = (old, new)
            observed_trees[environment] = observed_tree
            results[observed_ref] = new
            old_refs[observed_ref] = old

        # Add observed promotion lineage now that observation heads exist, then
        # refresh receipts once more so their desired revision matches the
        # final desired commit.
        for environment, (old, current) in list(desired_heads.items()):
            tree = desired_trees[environment]
            if not document_candidates(tree, "promotion"):
                continue
            rewrite_promotion_lineage(tree, desired_heads, observed_heads, desired_ref_by_environment)
            new = commit_tree(tree, current, f"Migrate {environment} observed promotion lineage")
            desired_heads[environment] = (old, new)
            desired_ref = heads_ref(desired_ref_by_environment[environment])
            results[desired_ref] = new
        for environment, (old, current) in list(observed_heads.items()):
            tree = observed_trees[environment]
            desired = desired_heads.get(environment)
            if desired is None:
                continue
            convert_observed(tree, desired[1], desired_trees[environment])
            new = commit_tree(tree, current, f"Migrate observed {environment} desired lineage")
            observed_heads[environment] = (old, new)
            results[heads_ref(next(item.observed for item in refs if item.environment == environment))] = new

    if apply:
        update_ref(source_ref, results[branch], old_source)
        for ref, old in old_refs.items():
            if ref == source_ref:
                continue
            update_ref(ref, results[ref], old)
        # update-ref moves the checked-out branch without updating its index or
        # working tree. The precondition above guarantees this cannot overwrite
        # pre-existing work, and leaves the checkout at the commit just created.
        git("reset", "--hard", results[branch])
        if push:
            assert remote is not None
            git("push", "--atomic", remote, *[f"{ref}:{ref}" for ref in results])
    return results


ROOT = Path(git("rev-parse", "--show-toplevel")).resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-name", required=True, help="DNS-1123 name for the generated Project resource")
    parser.add_argument("--apply", action="store_true", help="update local refs with migration commits")
    parser.add_argument("--push", action="store_true", help="push migrated refs atomically after --apply")
    args = parser.parse_args()
    results = migrate(project_name=args.project_name, apply=args.apply, push=args.push)
    print(json.dumps(results, indent=2, sort_keys=True))
    if not args.apply:
        print("Preview only. Re-run with --apply to update refs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
