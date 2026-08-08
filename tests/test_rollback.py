"""A rollback is a forward commit containing the complete desired-state input."""

import json
import subprocess
from pathlib import Path

import pytest

from gitopsctr import cli as deploy_release
from tests.conftest import write_test_document


def _write_json(path: Path, value: dict[str, object]) -> None:
    write_test_document(path, value)


def _specification(name: str, producer: str | None = None) -> dict:
    inputs = {}
    if producer:
        inputs["value"] = {
            "fromReceipt": {"unit": producer, "pointer": "/outputs/value"},
        }
    return {
        "schema": 1,
        "name": name,
        "driver": "terraform",
        "source": {"path": "infra/deploy"},
        **({"inputs": inputs} if inputs else {}),
    }


def _desired_unit(name: str, revision: str, value: str) -> dict:
    return {
        "schema": 1,
        "name": name,
        "driver": "terraform",
        "source": {
            "path": "infra/deploy",
            "revision": revision,
            "inputHash": f"sha256:{value}",
            "driverVersion": deploy_release.DRIVER_VERSIONS["terraform"],
        },
        "terraform": {"variables": {"value": value}},
    }


def _materialized_specification(name: str, producer: str | None = None) -> dict:
    values = {}
    if producer:
        values["value"] = {
            "fromReceipt": {"unit": producer, "pointer": "/outputs/value"},
        }
    return {
        "schema": 1,
        "name": name,
        "driver": "kubernetes-manifests",
        "source": {"path": "manifests"},
        "materialize": {
            "type": "helm",
            "releaseName": name,
            "namespace": "default",
            "values": values,
        },
        "delivery": {"mode": "direct", "kubeContext": "test"},
    }


def _materialized_desired_unit(name: str, revision: str, value: str) -> dict:
    return {
        "schema": 1,
        "name": name,
        "driver": "kubernetes-manifests",
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
    }


def _receipt(unit_path: Path, unit_name: str, revision: str) -> dict:
    return {
        "schema": 1,
        "unit": unit_name,
        "driver": "terraform",
        "desired": {
            "revision": revision,
            "unitBlob": deploy_release.file_blob(unit_path),
        },
        "applied": {"sourceRevision": revision},
        "outputs": {},
    }


def _promotion_document(revision: str) -> dict:
    return {
        "schema": 1,
        "source": {
            "environment": "staging",
            "desiredRef": "deploy/staging",
            "desiredRevision": revision,
            "observedRef": "observed/staging",
            "observedRevision": revision,
        },
        "specificationRevision": revision,
    }


def test_downstream_unit_closure_is_transitive_and_excludes_selected_units():
    documents = {
        "base": _specification("base"),
        "application": _specification("application", "base"),
        "frontend": _specification("frontend", "application"),
        "unrelated": _specification("unrelated"),
    }
    specifications = {
        name: deploy_release.parse_authored_unit_document(document, name) for name, document in documents.items()
    }

    assert deploy_release.downstream_unit_closure(specifications, ["base"]) == (
        "application",
        "frontend",
    )
    assert deploy_release.downstream_unit_closure(specifications, ["application"]) == ("frontend",)


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


def test_mixed_rollback_requires_a_fully_materialized_current_tree(tmp_path):
    source = tmp_path / "source"
    desired = tmp_path / "desired"
    _write_json(
        source / "deployment/environments/dev/environment.json",
        {"schema": 1, "name": "dev"},
    )
    _write_json(source / "deployment/environments/dev/units/base.json", _specification("base"))
    _write_json(
        source / "deployment/environments/dev/units/consumer.json",
        _specification("consumer", "base"),
    )
    _write_json(desired / "units/base.json", _desired_unit("base", "a" * 40, "current"))

    with pytest.raises(deploy_release.OperationError, match="current desired state.*not fully"):
        deploy_release.validate_materialized_desired(
            "dev",
            "b" * 40,
            desired,
            source,
            "current desired state",
        )


def _install_rollback_simulation(
    monkeypatch,
    gate: str = "none",
    current_promoted: bool = False,
    target_promoted: bool = False,
    materialized_payloads: bool = False,
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
            if materialized_payloads:
                payload = output / f"materialized/{name}"
                payload.mkdir(parents=True, exist_ok=True)
                (payload / "rendered.yaml").write_text(f"unit: {name}\nvalue: {value}\n")
                if value == "current":
                    (payload / "stale.yaml").write_text("stale: true\n")
                unit["materialization"] = {
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
        assert document["terraform"]["variables"]["value"] == value
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
        assert unit["materialization"]["path"] == f"materialized/{name}"
        payload_root = tmp_path / name
        for path, content in files.items():
            prefix = f"materialized/{name}/"
            if path.startswith(prefix):
                output = payload_root / path.removeprefix(prefix)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(content)
        assert unit["materialization"]["digest"] == deploy_release.materialization_tree_digest(payload_root)


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
    assert promotion["specificationRevision"] == revisions[expected_promotion]


def test_pull_request_gate_routes_rollback_through_candidate_submission(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    _write_json(candidate / "units/base.json", {"name": "base"})
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
    )

    assert result == ("d" * 40, outcome)
    assert captured[0][1:4] == (
        "rollback/prod/candidate",
        "deploy/prod",
        "c" * 40,
    )


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
        receipt["schema"] = 2
    elif corruption == "unit":
        receipt["unit"] = "other"
    elif corruption == "driver":
        receipt["driver"] = "oci-images"
    elif corruption == "revision":
        receipt["desired"]["revision"] = "not-a-revision"
    else:
        receipt.pop("outputs")
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


def test_full_rollback_rejects_environment_revision_mode_change(monkeypatch):
    revisions, _publications = _install_rollback_simulation(
        monkeypatch,
        current_promoted=True,
        target_promoted=False,
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

    with pytest.raises(deploy_release.OperationError, match="revision mode"):
        args.handler(args)


def test_full_rollback_rejects_historical_deployment_ref_change(monkeypatch):
    revisions, _publications = _install_rollback_simulation(monkeypatch)

    def refs(source_root, *_args, **_kwargs):
        if Path(source_root).name == "target-source":
            return "deploy/legacy", "observed/legacy"
        return "deploy/dev", "observed/dev"

    monkeypatch.setattr(deploy_release, "deployment_refs", refs)
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

    with pytest.raises(deploy_release.OperationError, match="deployment refs"):
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
