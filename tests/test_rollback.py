"""A rollback is a forward commit containing the complete desired-state input."""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from gitopsctr import controller as deploy_release
from tests.conftest import receipt_document, write_test_document
from tests.stack_deletion_support import stack_tree


def _write_json(path: Path, value: dict[str, object]) -> None:
    write_test_document(path, value)


def _specification(name: str, producer: str | None = None) -> dict:
    inputs = {}
    if producer:
        inputs["value"] = {
            "fromReceipt": {"unit": producer, "pointer": "/outputs/value"},
        }
    return {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "Terraform",
        "metadata": {"name": name},
        "spec": {
            "source": {"path": "infra/deploy"},
            **({"inputs": inputs} if inputs else {}),
        },
    }


def _desired_unit(name: str, revision: str, value: str) -> dict:
    return {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "Terraform",
        "metadata": deploy_release.ResourceMetadata.root_from_provenance(
            name, f"rollback-test:{value}:{name}", partition="application"
        ).document(profile="desired"),
        "spec": {
            "source": {
                "path": "infra/deploy",
                "revision": revision,
                "inputHash": f"sha256:{value}",
                "driverVersion": deploy_release.DRIVER_VERSIONS["terraform"],
            },
            "terraform": {"backend": {}, "variables": {"value": value}, "observeOutputs": []},
        },
    }


def _materialized_specification(name: str, producer: str | None = None) -> dict:
    values = {}
    if producer:
        values["value"] = {
            "fromReceipt": {"unit": producer, "pointer": "/outputs/value"},
        }
    return {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "KubernetesManifests",
        "metadata": {"name": name},
        "spec": {
            "source": {"path": "manifests"},
            "materialize": {
                "type": "helm",
                "releaseName": name,
                "namespace": "default",
                "values": values,
            },
            "delivery": {"mode": "direct", "kubeContext": "test"},
        },
    }


def _materialized_desired_unit(name: str, revision: str, value: str) -> dict:
    return {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "KubernetesManifests",
        "metadata": deploy_release.ResourceMetadata.root_from_provenance(
            name, f"rollback-test:{value}:{name}", partition="application"
        ).document(profile="desired"),
        "spec": {
            "source": {
                "path": "manifests",
                "revision": revision,
                "inputHash": f"sha256:{value}",
                "driverVersion": deploy_release.DRIVER_VERSIONS["kubernetes-manifests"],
            },
            "materialize": {
                "type": "helm",
                "releaseName": name,
                "namespace": "default",
                "values": {"value": value},
            },
            "delivery": {"mode": "direct", "kubeContext": "test"},
        },
    }


def _receipt(unit_path: Path, unit_name: str, revision: str) -> dict:
    return receipt_document(
        "terraform",
        unit_name,
        {"revision": revision, "unitBlob": deploy_release.file_blob(unit_path)},
        {"applied": {"sourceRevision": revision}, "outputs": {}},
    )


def _promotion_document(revision: str) -> dict:
    return {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "Promotion",
        "metadata": {"name": "staging"},
        "spec": {
            "source": {
                "environment": "staging",
                "desiredRef": "deploy/staging",
                "desiredRevision": revision,
                "observedRef": "observed/staging",
                "observedRevision": revision,
            },
            "specificationRevision": revision,
        },
    }


def _template_only(root: Path) -> None:
    stack_tree(root)
    (root / "stacks/preview.json").unlink()
    (root / "units/preview--preview-app.json").unlink()


def test_full_stack_rollback_rejects_recreated_root_identity(tmp_path: Path):
    current = tmp_path / "current"
    target = tmp_path / "target"
    _template_only(current)
    _template_only(target)
    target_document = json.loads((target / "stack-templates/preview.json").read_text())
    target_document["metadata"]["uid"] = "d1-template-new"
    (target / "stack-templates/preview.json").write_text(json.dumps(target_document))

    with pytest.raises(deploy_release.OperationError, match="cross the current StackTemplate"):
        deploy_release.validate_full_rollback_stack_aggregate(current, target)


def test_full_stack_rollback_rejects_resurrection_of_finalized_root(tmp_path: Path):
    current = tmp_path / "current"
    target = tmp_path / "target"
    current.mkdir()
    _template_only(target)
    deploy_release.write_resource_incarnation_tombstone(
        current,
        deploy_release.ResourceIncarnationTombstone(
            api_version=deploy_release.CORE_API_VERSION,
            kind="StackTemplate",
            name="preview",
            uid="d1-template",
            deletion_generation=1,
        ),
    )

    with pytest.raises(deploy_release.OperationError, match="resurrect finalized StackTemplate"):
        deploy_release.validate_full_rollback_stack_aggregate(current, target)


def test_targeted_rollback_rejects_stack_owned_unit_before_publication(tmp_path: Path, monkeypatch):
    current = tmp_path / "current"
    stack_tree(current)
    unit_name = "preview--preview-app"
    owned = deploy_release.load_desired_unit(current / "units/preview--preview-app.json", unit_name)
    inventory = deploy_release.RollbackDesiredInventory({unit_name: owned}, {unit_name: ()})
    published = False

    def observed_tree(_ref: str, output: Path) -> str:
        shutil.copytree(current, output)
        return "c" * 40

    monkeypatch.setattr(deploy_release, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(deploy_release, "deployment_refs", lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"))
    monkeypatch.setattr(deploy_release, "observed_tree", observed_tree)
    monkeypatch.setattr(deploy_release, "resolve_ref", lambda *_args: "a" * 40)
    monkeypatch.setattr(deploy_release, "effect_lease_ref", lambda *_args: None)
    monkeypatch.setattr(deploy_release, "git", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=""))
    monkeypatch.setattr(deploy_release, "materialize_revision", lambda _revision, output: output.mkdir())
    monkeypatch.setattr(deploy_release, "validate_rollback_desired_inventory", lambda *_args: inventory)

    def fail_publish(*_args, **_kwargs):
        nonlocal published
        published = True
        raise AssertionError("targeted Stack-owned rollback must stop before publication")

    monkeypatch.setattr(deploy_release, "publish_desired_change", fail_publish)
    args = deploy_release.build_parser().parse_args(
        [
            "rollback",
            "--environment",
            "dev",
            "--to-desired-revision",
            "a" * 40,
            "--unit",
            unit_name,
            "--reason",
            "avoid historical Stack child",
        ]
    )

    with pytest.raises(deploy_release.OperationError, match=r"targeted rollback of Stack-owned Unit\(s\)"):
        deploy_release.command_rollback(args)
    assert not published


def test_downstream_unit_closure_is_transitive_and_excludes_selected_units():
    units = {
        name: deploy_release.parse_desired_unit_document(_desired_unit(name, "a" * 40, name), name)
        for name in ("base", "application", "frontend", "unrelated")
    }
    inventory = deploy_release.RollbackDesiredInventory(
        units,
        {
            "base": (),
            "application": ("base",),
            "frontend": ("application",),
            "unrelated": (),
        },
    )

    assert deploy_release._downstream_desired_unit_closure(inventory, ["base"]) == (
        "application",
        "frontend",
    )
    assert deploy_release._downstream_desired_unit_closure(inventory, ["application"]) == ("frontend",)


def test_clean_rollback_target_requires_one_matching_observed_snapshot(tmp_path, monkeypatch):
    desired = tmp_path / "desired"
    first = desired / "units/first.json"
    second = desired / "units/second.json"
    _write_json(first, _desired_unit("first", "c" * 40, "first"))
    _write_json(second, _desired_unit("second", "c" * 40, "second"))
    revisions = ["a" * 40, "b" * 40]
    monkeypatch.setattr(deploy_release, "fetch_ref", lambda _ref: revisions[-1])
    monkeypatch.setattr(
        deploy_release,
        "git",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(_args, 0, "\n".join(revisions) + "\n", ""),
    )

    def materialize(revision, output):
        unit = first if revision == revisions[0] else second
        _write_json(output / f"units/{unit.stem}.json", _receipt(unit, unit.stem, revision))

    monkeypatch.setattr(deploy_release, "materialize_revision", materialize)

    with pytest.raises(deploy_release.OperationError, match="never fully clean"):
        deploy_release.find_clean_observed_snapshot("observed/dev", desired, ["first", "second"], tmp_path / "history")


def test_clean_rollback_target_returns_the_matching_observed_revision(tmp_path, monkeypatch):
    desired = tmp_path / "desired"
    units = [desired / "units/first.json", desired / "units/second.json"]
    for unit in units:
        _write_json(unit, _desired_unit(unit.stem, "c" * 40, unit.stem))
    revision = "a" * 40
    monkeypatch.setattr(deploy_release, "fetch_ref", lambda _ref: revision)
    monkeypatch.setattr(
        deploy_release,
        "git",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(_args, 0, revision + "\n", ""),
    )

    def materialize(_revision, output):
        for unit in units:
            _write_json(
                output / f"units/{unit.stem}.json",
                _receipt(unit, unit.stem, revision),
            )

    monkeypatch.setattr(deploy_release, "materialize_revision", materialize)

    assert (
        deploy_release.find_clean_observed_snapshot("observed/dev", desired, ["first", "second"], tmp_path / "history")
        == revision
    )


def test_rollback_parser_supports_full_or_repeated_unit_scope():
    args = deploy_release.build_parser().parse_args(
        [
            "rollback",
            "--environment",
            "prod",
            "--to-desired-revision",
            "a" * 40,
            "--unit",
            "application",
            "--unit",
            "frontend",
            "--reason",
            "Known-bad release",
            "--dry",
        ]
    )

    assert args.environment == "prod"
    assert args.to_desired_revision == "a" * 40
    assert args.unit == ["application", "frontend"]
    assert args.reason == "Known-bad release"
    assert args.dry is True


def test_targeted_rollback_rejects_missing_persisted_dependency(tmp_path):
    desired = tmp_path / "desired"
    consumer = _desired_unit("consumer", "a" * 40, "current")
    consumer["spec"]["resolvedInputs"] = {"receipts": {"base": "receipt-blob"}}
    _write_json(desired / "units/consumer.json", consumer)

    with pytest.raises(deploy_release.OperationError, match="depends on missing Unit.*base"):
        deploy_release.validate_rollback_desired_inventory(
            "b" * 40,
            desired,
            "current desired state",
        )


def _install_rollback_simulation(
    monkeypatch,
    gate: str = "none",
    current_promoted: bool = False,
    target_promoted: bool = False,
    materialized_payloads: bool = False,
    canonical_payloads: bool = False,
    current_cleanup: bool = False,
    current_blocked: bool = False,
    target_cleanup: bool = False,
):
    revisions = {
        "target": "a" * 40,
        "target_specification": "b" * 40,
        "current": "c" * 40,
        "current_specification": "d" * 40,
        "observed": "e" * 40,
        "published": "f" * 40,
    }
    publications = []
    specification = _materialized_specification if materialized_payloads else _specification
    specifications = {
        "base": specification("base"),
        "consumer": specification("consumer", "base"),
        "unrelated": specification("unrelated"),
    }

    def write_source(output, promoted):
        environment = {"schema": 1, "name": "dev", "changeGate": gate}
        if promoted:
            environment["promotion"] = {"allowedSources": ["staging"]}
        _write_json(output / "deployment/environments/dev/environment.json", environment)
        for name, specification in specifications.items():
            _write_json(
                output / f"deployment/environments/dev/units/{name}.json",
                specification,
            )

    def write_desired(output, revision, value, promoted):
        for name in specifications:
            unit = (
                _materialized_desired_unit(name, revision, value)
                if materialized_payloads
                else _desired_unit(name, revision, value)
            )
            if name == "consumer":
                unit["spec"]["resolvedInputs"] = {"receipts": {"base": f"receipt:{value}"}}
            if materialized_payloads:
                payload = output / f"materialized/{name}"
                payload.mkdir(parents=True, exist_ok=True)
                (payload / "rendered.yaml").write_text(f"unit: {name}\nvalue: {value}\n")
                if value == "current":
                    (payload / "stale.yaml").write_text("stale: true\n")
                unit["spec"]["materialization"] = {
                    "path": f"materialized/{name}",
                    "digest": deploy_release.materialization_tree_digest(payload),
                    "mediaType": "application/yaml",
                    "metadata": {
                        "renderer": "helm",
                        "releaseName": name,
                        "namespace": "default",
                        "inventory": [],
                    },
                }
            if canonical_payloads:
                historical = deploy_release.parse_desired_unit_document(unit, name)
                unit = deploy_release.serialize_unit_document(
                    historical.with_metadata(
                        deploy_release.ResourceMetadata.root_from_provenance(
                            name, f"rollback-test:{value}:{name}", partition="application"
                        )
                    )
                )
            _write_json(output / f"units/{name}.json", unit)
        if promoted:
            _write_json(output / "promotion.json", _promotion_document(revision))

    def materialize(revision, output):
        if revision == revisions["target"]:
            write_desired(
                output,
                revisions["target_specification"],
                "rollback",
                target_promoted,
            )
            if target_cleanup:
                _write_json(
                    output / ".gitopsctr/cleanup/units/unrelated.json",
                    {
                        "schema": 1,
                        "kind": "OpaqueCleanupRoot",
                        "metadata": deploy_release.ResourceMetadata.root_from_provenance(
                            "unrelated", "rollback-target-cleanup", partition="application"
                        ).document(profile="desired"),
                        "payload": {"name": "unrelated", "driver": "terraform"},
                    },
                )
                _write_json(
                    output / ".gitopsctr/transition-blocks.json",
                    {"schema": 1, "blocks": {"unrelated": "historical stale block"}},
                )
        elif revision == revisions["target_specification"]:
            write_source(output, target_promoted)
        elif revision == revisions["current_specification"]:
            write_source(output, current_promoted)
        else:
            raise AssertionError(f"unexpected materialization: {revision}")

    def observed_tree(ref, output):
        assert ref == "deploy/dev"
        write_desired(
            output,
            revisions["current_specification"],
            "current",
            current_promoted,
        )
        if current_blocked:
            base_path = output / "units/base.json"
            base = deploy_release.parse_desired_unit_document(deploy_release.load_json(base_path), "base")
            _write_json(
                base_path,
                deploy_release.serialize_unit_document(
                    base.with_metadata(
                        deploy_release.ResourceMetadata.root_from_provenance(
                            "base", "rollback-current-blocked", partition="application"
                        )
                    )
                ),
            )
            _write_json(
                output / ".gitopsctr/transition-blocks.json",
                {"schema": 1, "blocks": {"base": "current parseable transition is blocked"}},
            )
        if current_cleanup:
            cleanup_payload = _desired_unit("base", revisions["current_specification"], "current-cleanup")
            (output / "units/base.json").unlink()
            _write_json(
                output / ".gitopsctr/cleanup/units/base.json",
                {
                    "schema": 1,
                    "kind": "OpaqueCleanupRoot",
                    "metadata": deploy_release.ResourceMetadata.root_from_provenance(
                        "base", "rollback-current-cleanup", partition="application"
                    ).document(profile="desired"),
                    "payload": cleanup_payload,
                },
            )
            _write_json(
                output / ".gitopsctr/transition-blocks.json",
                {"schema": 1, "blocks": {"base": "current opaque cleanup root retained"}},
            )
        return revisions["current"]

    def publish(ref, directory, parent, message):
        publications.append(
            {
                "ref": ref,
                "files": deploy_release.directory_files(directory),
                "parent": parent,
                "message": message,
            }
        )
        return revisions["published"]

    monkeypatch.setattr(
        deploy_release,
        "deployment_refs",
        lambda *_args, **_kwargs: ("deploy/dev", "observed/dev"),
    )
    monkeypatch.setattr(deploy_release, "observed_tree", observed_tree)
    monkeypatch.setattr(deploy_release, "resolve_ref", lambda *_args: revisions["target"])
    monkeypatch.setattr(deploy_release, "materialize_revision", materialize)
    monkeypatch.setattr(
        deploy_release,
        "git",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(_args, 0, "", ""),
    )
    monkeypatch.setattr(
        deploy_release,
        "desired_specification_revision",
        lambda revision, *_args: (
            revisions["current_specification"]
            if revision == revisions["current"]
            else revisions["target_specification"]
        ),
    )
    monkeypatch.setattr(
        deploy_release,
        "find_clean_observed_snapshot",
        lambda *_args: revisions["observed"],
    )
    monkeypatch.setattr(deploy_release, "change_gate", lambda *_args: gate)
    monkeypatch.setattr(deploy_release, "publish_tree", publish)
    return revisions, publications


@pytest.mark.parametrize(
    ("units", "requested_label", "materialized_label"),
    [
        ([], "all", "base, consumer, unrelated"),
        (["base"], "base", "base, consumer"),
    ],
)
def test_rollback_publishes_complete_forward_desired_state(
    units, requested_label, materialized_label, monkeypatch, capsys
):
    revisions, publications = _install_rollback_simulation(monkeypatch)
    arguments = [
        "rollback",
        "--environment",
        "dev",
        "--to-desired-revision",
        revisions["target"],
        "--reason",
        "Known-bad release",
    ]
    for unit in units:
        arguments.extend(["--unit", unit])

    args = deploy_release.build_parser().parse_args(arguments)
    args.handler(args)

    assert capsys.readouterr().out == revisions["published"] + "\n"
    assert len(publications) == 1
    publication = publications[0]
    assert publication["ref"] == "deploy/dev"
    assert publication["parent"] == revisions["current"]
    assert set(publication["files"]) == {
        "units/base.json",
        "units/consumer.json",
        "units/unrelated.json",
    }
    expected = {
        "base": "rollback",
        "consumer": "rollback",
        "unrelated": "rollback" if not units else "current",
    }
    for unit, value in expected.items():
        document = json.loads(publication["files"][f"units/{unit}.json"])
        assert document["apiVersion"] == "unit.gitopsctr.io/v1"
        assert document["metadata"]["uid"]
        assert document["metadata"]["labels"] == {"gitopsctr.io/partition": "application"}
        assert "driver" not in document
        assert document["spec"]["terraform"]["variables"]["value"] == value
    message = publication["message"]
    assert f"Target-Desired-Revision: {revisions['target']}" in message
    assert f"Target-Observed-Revision: {revisions['observed']}" in message
    assert f"Requested-Units: {requested_label}" in message
    assert f"Materialized-Units: {materialized_label}" in message
    assert "Reason: Known-bad release" in message


@pytest.mark.parametrize(
    ("units", "historical_units"),
    [([], {"base", "consumer", "unrelated"}), (["base"], {"base", "consumer"})],
)
def test_rollback_copies_exact_historical_payloads_and_removes_stale_files(
    units, historical_units, tmp_path, monkeypatch
):
    revisions, publications = _install_rollback_simulation(monkeypatch, materialized_payloads=True)
    arguments = [
        "rollback",
        "--environment",
        "dev",
        "--to-desired-revision",
        revisions["target"],
        "--reason",
        "Known-bad release",
    ]
    for unit in units:
        arguments.extend(["--unit", unit])

    args = deploy_release.build_parser().parse_args(arguments)
    args.handler(args)

    files = publications[0]["files"]
    for name in {"base", "consumer", "unrelated"}:
        expected_value = "rollback" if name in historical_units else "current"
        assert files[f"materialized/{name}/rendered.yaml"] == f"unit: {name}\nvalue: {expected_value}\n".encode()
        assert (f"materialized/{name}/stale.yaml" in files) is (name not in historical_units)
        unit = json.loads(files[f"units/{name}.json"])
        assert unit["spec"]["materialization"]["path"] == f"materialized/{name}"
        payload_root = tmp_path / name
        for path, content in files.items():
            prefix = f"materialized/{name}/"
            if path.startswith(prefix):
                output = payload_root / path.removeprefix(prefix)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(content)
        assert unit["spec"]["materialization"]["digest"] == deploy_release.materialization_tree_digest(payload_root)


def test_rollback_preserves_current_canonical_identity_over_historical_identity(monkeypatch, capsys):
    revisions, publications = _install_rollback_simulation(monkeypatch, canonical_payloads=True)
    args = deploy_release.build_parser().parse_args(
        [
            "rollback",
            "--environment",
            "dev",
            "--to-desired-revision",
            revisions["target"],
            "--reason",
            "Known-bad release",
        ]
    )

    args.handler(args)

    capsys.readouterr()
    for name in ("base", "consumer", "unrelated"):
        document = json.loads(publications[0]["files"][f"units/{name}.json"])
        current_uid = deploy_release.ResourceMetadata.root_from_provenance(
            name, f"rollback-test:current:{name}", partition="application"
        ).uid
        historical_uid = deploy_release.ResourceMetadata.root_from_provenance(
            name, f"rollback-test:rollback:{name}", partition="application"
        ).uid
        assert document["metadata"]["uid"] == current_uid
        assert document["metadata"]["uid"] != historical_uid


def test_rollback_restores_historical_payload_with_new_uid_after_finalization(tmp_path):
    current = tmp_path / "current"
    candidate = tmp_path / "candidate"
    historical_path = candidate / "units/application.json"
    current.mkdir()
    _write_json(historical_path, _desired_unit("application", "b" * 40, "historical"))
    finalized = deploy_release.ResourceIncarnationTombstone(
        api_version="unit.gitopsctr.io/v1",
        kind="Test",
        name="application",
        uid="d1-finalized-application",
        deletion_generation=1,
    )
    deploy_release.write_resource_incarnation_tombstone(current, finalized)

    deploy_release.merge_current_cleanup_state(current, candidate)
    deploy_release.canonicalize_rollback_unit(
        historical_path,
        current / "units/application.json",
        deploy_release.finalized_incarnation_for_resource(
            deploy_release.load_resource_incarnation_tombstones(candidate),
            "unit.gitopsctr.io/v1",
            "Test",
            "application",
        ),
    )

    restored = deploy_release.load_desired_unit(historical_path, "application")
    assert restored.metadata.uid != finalized.uid
    assert restored.metadata.partition == "application"
    assert (
        deploy_release.load_resource_incarnation_tombstones(candidate)[("unit.gitopsctr.io/v1", "Test", "application")]
        == finalized
    )


def test_full_rollback_preserves_current_opaque_cleanup_root(monkeypatch, capsys):
    revisions, publications = _install_rollback_simulation(monkeypatch, current_cleanup=True)
    args = deploy_release.build_parser().parse_args(
        [
            "rollback",
            "--environment",
            "dev",
            "--to-desired-revision",
            revisions["target"],
            "--reason",
            "Known-bad release",
        ]
    )

    args.handler(args)

    capsys.readouterr()
    files = publications[0]["files"]
    assert "units/base.json" not in files
    cleanup = json.loads(files[".gitopsctr/cleanup/units/base.json"])
    assert cleanup["payload"]["spec"]["source"]["revision"] == revisions["current_specification"]
    assert json.loads(files[".gitopsctr/transition-blocks.json"])["blocks"]["base"] == (
        "current opaque cleanup root retained"
    )


def test_full_rollback_preserves_current_parseable_blocked_unit_and_materialization(monkeypatch, capsys):
    revisions, publications = _install_rollback_simulation(
        monkeypatch,
        materialized_payloads=True,
        current_blocked=True,
        target_cleanup=True,
    )
    args = deploy_release.build_parser().parse_args(
        [
            "rollback",
            "--environment",
            "dev",
            "--to-desired-revision",
            revisions["target"],
            "--reason",
            "Known-bad release",
        ]
    )

    args.handler(args)

    capsys.readouterr()
    files = publications[0]["files"]
    base = json.loads(files["units/base.json"])
    assert (
        base["metadata"]["uid"]
        == deploy_release.ResourceMetadata.root_from_provenance(
            "base", "rollback-current-blocked", partition="application"
        ).uid
    )
    assert base["spec"]["materialization"]["path"] == "materialized/base"
    assert files["materialized/base/rendered.yaml"] == b"unit: base\nvalue: current\n"
    assert ".gitopsctr/cleanup/units/unrelated.json" not in files
    assert json.loads(files[".gitopsctr/transition-blocks.json"]) == {
        "blocks": {"base": "current parseable transition is blocked"},
        "schema": 1,
    }


@pytest.mark.parametrize("different_payload", [False, True])
def test_full_rollback_stack_owned_block_overlay_keeps_target_payload_and_blocks(
    tmp_path: Path,
    different_payload: bool,
):
    current = tmp_path / "current"
    target = tmp_path / "target"
    stack_tree(current)
    stack_tree(target)
    current_unit_path = current / "units/preview--preview-app.json"
    if different_payload:
        current_document = json.loads(current_unit_path.read_text())
        current_document["spec"]["terraform"] = {"backend": {}, "variables": {"value": "current"}}
        current_unit_path.write_text(json.dumps(current_document))
    deploy_release.write_desired_transition_blocks(
        current,
        {"preview--preview-app": "current Stack-owned transition is blocked"},
    )
    candidate = tmp_path / "candidate"
    shutil.copytree(target, candidate)

    deploy_release.merge_current_cleanup_state(
        current,
        candidate,
        preserve_target_stack_semantics=True,
    )

    target_unit = deploy_release.load_desired_unit(target / "units/preview--preview-app.json", "preview--preview-app")
    candidate_unit = deploy_release.load_desired_unit(
        candidate / "units/preview--preview-app.json",
        "preview--preview-app",
    )
    assert candidate_unit.spec == target_unit.spec
    assert deploy_release.load_desired_transition_blocks(candidate) == {}


@pytest.mark.parametrize(
    ("units", "expected_promotion"),
    [([], "target_specification"), (["base"], "current_specification")],
)
def test_rollback_keeps_the_promotion_that_drives_its_resulting_tree(units, expected_promotion, monkeypatch):
    revisions, publications = _install_rollback_simulation(
        monkeypatch,
        current_promoted=True,
        target_promoted=True,
    )
    arguments = [
        "rollback",
        "--environment",
        "dev",
        "--to-desired-revision",
        revisions["target"],
        "--reason",
        "Known-bad release",
    ]
    for unit in units:
        arguments.extend(["--unit", unit])

    args = deploy_release.build_parser().parse_args(arguments)
    args.handler(args)

    promotion = json.loads(publications[0]["files"]["promotion.json"])
    assert promotion["spec"]["specificationRevision"] == revisions[expected_promotion]


def test_pull_request_gate_routes_rollback_through_candidate_submission(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    current = tmp_path / "current"
    current.mkdir()
    candidate_unit = deploy_release.parse_desired_unit_document(_desired_unit("base", "c" * 40, "candidate"), "base")
    _write_json(
        candidate / "units/base.json",
        deploy_release.serialize_unit_document(
            candidate_unit.with_metadata(deploy_release.ResourceMetadata.new_root("base", partition="application"))
        ),
    )
    captured = []
    outcome = deploy_release.ChangeRequestResult(status="created", url="https://github.example/pull/1")
    monkeypatch.setattr(deploy_release, "change_gate", lambda *_args: "pullRequest")

    def publish(*args):
        captured.append(args)
        return "d" * 40, outcome

    monkeypatch.setattr(deploy_release, "publish_change_candidate", publish)

    result = deploy_release.publish_desired_change(
        "prod",
        candidate,
        "deploy/prod",
        "c" * 40,
        "rollback/prod/candidate",
        "Rollback prod",
        "Roll back prod",
        "Reason",
        False,
        current,
    )

    assert result == ("d" * 40, outcome)
    assert captured[0][1:4] == (
        "rollback/prod/candidate",
        "deploy/prod",
        "c" * 40,
    )


@pytest.mark.parametrize(
    ("override", "expected_ref"),
    [
        (None, "gitopsctr/candidates/dev/candidate123"),
        ("manual/rollback", "manual/rollback"),
    ],
)
def test_gated_rollback_uses_candidate_template_or_exact_override(override, expected_ref, monkeypatch):
    revisions, _publications = _install_rollback_simulation(monkeypatch, gate="pullRequest")
    captured = []
    outcome = deploy_release.ChangeRequestResult(status="created", url="https://github.example/pull/1")
    monkeypatch.setattr(deploy_release, "candidate_identifier", lambda *_args, **_kwargs: "candidate123")

    def publish(candidate, candidate_ref, target_ref, target_revision, *_args):
        captured.append((candidate_ref, target_ref, target_revision, deploy_release.directory_files(candidate)))
        return revisions["published"], outcome

    monkeypatch.setattr(deploy_release, "publish_change_candidate", publish)
    arguments = [
        "rollback",
        "--environment",
        "dev",
        "--to-desired-revision",
        revisions["target"],
        "--reason",
        "Known-bad release",
    ]
    if override:
        arguments.extend(("--candidate-ref", override))

    args = deploy_release.build_parser().parse_args(arguments)
    args.handler(args)

    assert captured[0][:3] == (expected_ref, "deploy/dev", revisions["current"])


def test_direct_rollback_rejects_candidate_ref_override(monkeypatch):
    revisions, _publications = _install_rollback_simulation(monkeypatch, gate="none")
    args = deploy_release.build_parser().parse_args(
        [
            "rollback",
            "--environment",
            "dev",
            "--to-desired-revision",
            revisions["target"],
            "--reason",
            "Known-bad release",
            "--candidate-ref",
            "manual/rollback",
        ]
    )

    with pytest.raises(deploy_release.OperationError, match="requires changeGate pullRequest"):
        args.handler(args)


@pytest.mark.parametrize(
    "corruption",
    ["schema", "unit", "driver", "revision", "semantic-result"],
)
def test_historical_rollback_evidence_rejects_invalid_receipts(corruption, tmp_path):
    desired = tmp_path / "desired"
    observed = tmp_path / "observed"
    unit_path = desired / "units/base.json"
    _write_json(unit_path, _desired_unit("base", "a" * 40, "stable"))
    receipt = _receipt(unit_path, "base", "b" * 40)
    if corruption == "schema":
        receipt["apiVersion"] = "gitopsctr.io/v2"
    elif corruption == "unit":
        receipt["metadata"]["name"] = "other"
    elif corruption == "driver":
        receipt["spec"]["subject"]["kind"] = "OciImages"
    elif corruption == "revision":
        receipt["spec"]["desired"]["revision"] = "not-a-revision"
    else:
        receipt["status"]["result"].pop("outputs")
    _write_json(observed / "units/base.json", receipt)

    assert deploy_release.historical_receipt_matches(desired, observed, "base") is False


def test_rollback_rechecks_target_ancestry_against_captured_current_head(monkeypatch):
    revisions, _publications = _install_rollback_simulation(monkeypatch)
    monkeypatch.setattr(
        deploy_release,
        "git",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(_args, 1, "", ""),
    )
    args = deploy_release.build_parser().parse_args(
        [
            "rollback",
            "--environment",
            "dev",
            "--to-desired-revision",
            revisions["target"],
            "--reason",
            "Known-bad release",
        ]
    )

    with pytest.raises(deploy_release.OperationError, match="not ancestral"):
        args.handler(args)


def test_rollback_dry_run_writes_no_deployment_ref(monkeypatch, capsys):
    revisions, publications = _install_rollback_simulation(monkeypatch)
    args = deploy_release.build_parser().parse_args(
        [
            "rollback",
            "--environment",
            "dev",
            "--to-desired-revision",
            revisions["target"],
            "--unit",
            "base",
            "--reason",
            "Known-bad release",
            "--dry",
        ]
    )

    args.handler(args)

    assert publications == []
    preview = json.loads(capsys.readouterr().out)
    assert preview["targetDesiredRevision"] == revisions["target"]
    assert preview["requestedUnits"] == ["base"]
    assert preview["materializedUnits"] == ["base", "consumer"]
