from __future__ import annotations

import json
from pathlib import Path

import pytest

from gitopsctr import controller
from gitopsctr.contracts import DesiredLifecycle, DesiredOwnerReference, LifecycleManagement
from gitopsctr.errors import OperationError
from gitopsctr.resources import ResourceMetadata, validate_desired_resource_graph


def authored_terraform(name: str) -> dict[str, object]:
    return {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "Terraform",
        "metadata": {"name": name},
        "spec": {"source": {"path": "."}},
    }


def legacy_desired(name: str, revision: str = "a" * 40) -> dict[str, object]:
    return {
        "name": name,
        "driver": "terraform",
        "source": {
            "path": ".",
            "revision": revision,
            "inputHash": "sha256:inputs",
            "driverVersion": controller.DRIVER_VERSIONS["terraform"],
        },
    }


def test_desired_round_trip_assigns_canonical_identity_and_authority():
    parsed = controller.parse_desired_unit_document(legacy_desired("infra"), "infra")

    assert parsed.is_legacy_compatibility
    with pytest.raises(OperationError, match="must be canonical"):
        controller.serialize_unit_document(parsed)

    adopted = parsed.with_metadata(ResourceMetadata.new_source_tracked(parsed.name))
    document = controller.serialize_unit_document(adopted)
    assert document["metadata"]["uid"]
    assert document["metadata"]["lifecycle"] == {"management": {"mode": "sourceTracked"}}

    round_tripped = controller.parse_desired_unit_document(document, "infra")
    assert not round_tripped.is_legacy_compatibility
    assert round_tripped.metadata.uid == document["metadata"]["uid"]


def test_authored_metadata_rejects_lifecycle_fields():
    document = authored_terraform("infra")
    document["metadata"] = {
        "name": "infra",
        "uid": "uid-1",
        "lifecycle": {"management": {"mode": "sourceTracked"}},
    }

    with pytest.raises(OperationError, match="authored unit metadata may contain only name"):
        controller.parse_authored_unit_document(document, "infra")


@pytest.mark.parametrize("field", ["uid", "lifecycle"])
def test_desired_metadata_rejects_explicit_null_lifecycle_fields(field):
    document = {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "Terraform",
        "metadata": {"name": "infra", field: None},
        "spec": {
            "source": {
                "path": ".",
                "revision": "a" * 40,
                "inputHash": "sha256:inputs",
                "driverVersion": controller.DRIVER_VERSIONS["terraform"],
            }
        },
    }

    with pytest.raises(OperationError, match="null values"):
        controller.parse_desired_unit_document(document, "infra")


def test_lifecycle_model_requires_exactly_one_authority():
    with pytest.raises(ValueError, match="exactly one"):
        DesiredLifecycle()
    with pytest.raises(ValueError, match="exactly one"):
        DesiredLifecycle(
            management=LifecycleManagement(mode="direct"),
            owner=DesiredOwnerReference(
                apiVersion="unit.gitopsctr.io/v1",
                kind="Terraform",
                name="owner",
                uid="uid-owner",
            ),
        )


def test_owner_uid_fencing_and_cycles_are_validated():
    owner_resource = controller.parse_desired_unit_document(legacy_desired("owner"), "owner").with_metadata(
        ResourceMetadata.new_source_tracked("owner")
    )
    owner_document = controller.serialize_unit_document(owner_resource)
    owner_uid = owner_document["metadata"]["uid"]
    child_document = {
        **legacy_desired("child"),
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "Terraform",
        "metadata": {
            "name": "child",
            "uid": "uid-child",
            "lifecycle": {
                "owner": {
                    "apiVersion": "unit.gitopsctr.io/v1",
                    "kind": "Terraform",
                    "name": "owner",
                    "uid": owner_uid,
                }
            },
        },
        "spec": {"source": {"path": ".", "revision": "a" * 40, "driverVersion": 2}},
    }
    owner_resource = controller.parse_desired_unit_document(owner_document, "owner")
    child_resource = controller.parse_desired_unit_document(child_document, "child")
    validate_desired_resource_graph(
        {
            ("unit.gitopsctr.io/v1", "Terraform", "owner"): owner_resource,
            ("unit.gitopsctr.io/v1", "Terraform", "child"): child_resource,
        }
    )

    bad_child = dict(child_document)
    bad_child["metadata"] = {
        **child_document["metadata"],
        "lifecycle": {
            "owner": {
                **child_document["metadata"]["lifecycle"]["owner"],
                "uid": "uid-wrong",
            }
        },
    }
    with pytest.raises(ValueError, match="different UID"):
        validate_desired_resource_graph(
            {
                ("unit.gitopsctr.io/v1", "Terraform", "owner"): owner_resource,
                ("unit.gitopsctr.io/v1", "Terraform", "child"): controller.parse_desired_unit_document(
                    bad_child, "child"
                ),
            }
        )

    cycle_document = dict(child_document)
    cycle_document["metadata"] = {
        **child_document["metadata"],
        "lifecycle": {
            "owner": {
                **child_document["metadata"]["lifecycle"]["owner"],
                "name": "child",
                "uid": "uid-child",
            }
        },
    }
    with pytest.raises(ValueError, match="acyclic"):
        validate_desired_resource_graph(
            {
                ("unit.gitopsctr.io/v1", "Terraform", "child"): controller.parse_desired_unit_document(
                    cycle_document, "child"
                )
            }
        )

    with pytest.raises(ValueError, match="mapping key"):
        validate_desired_resource_graph({("wrong", "Terraform", "owner"): owner_resource})

    duplicate_owner = owner_resource.with_metadata(ResourceMetadata.new_source_tracked("owner"))
    with pytest.raises(ValueError, match="duplicate desired resource identity"):
        validate_desired_resource_graph(
            {
                ("unit.gitopsctr.io/v1", "Terraform", "owner"): owner_resource,
                ("wrong", "Terraform", "duplicate"): duplicate_owner,
            }
        )


def test_legacy_resources_are_compatibility_roots_but_not_owner_targets():
    legacy_owner = controller.parse_desired_unit_document(legacy_desired("owner"), "owner")
    validate_desired_resource_graph({("unit.gitopsctr.io/v1", "Terraform", "owner"): legacy_owner})

    child_document = legacy_desired("child")
    child_document["apiVersion"] = "unit.gitopsctr.io/v1"
    child_document["kind"] = "Terraform"
    child_document["spec"] = {"source": child_document.pop("source")}
    child_document["metadata"] = {
        "name": "child",
        "uid": "uid-child",
        "lifecycle": {
            "owner": {
                "apiVersion": "unit.gitopsctr.io/v1",
                "kind": "Terraform",
                "name": "owner",
                "uid": "uid-legacy-owner",
            }
        },
    }
    child = controller.parse_desired_unit_document(child_document, "child")
    with pytest.raises(ValueError, match="legacy compatibility root"):
        validate_desired_resource_graph(
            {
                ("unit.gitopsctr.io/v1", "Terraform", "owner"): legacy_owner,
                ("unit.gitopsctr.io/v1", "Terraform", "child"): child,
            }
        )


def test_build_candidate_retains_uid_and_source_absent_cleanup_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source_root = tmp_path / "source"
    current = tmp_path / "current"
    observed = tmp_path / "observed"
    candidate = tmp_path / "candidate"
    source_root.mkdir()
    (source_root / "gitopsctr.yaml").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Project",
                "metadata": {"name": "test"},
                "spec": {"effectLease": None},
            }
        )
    )
    current_units = current / "units"
    current_units.mkdir(parents=True)
    observed.mkdir()

    existing = controller.parse_desired_unit_document(legacy_desired("infra"), "infra")
    existing_document = controller.serialize_unit_document(
        existing.with_metadata(ResourceMetadata.new_source_tracked(existing.name))
    )
    (current_units / "infra.json").write_text(json.dumps(existing_document))
    (current_units / "orphan.json").write_text(json.dumps(legacy_desired("orphan")))
    existing_uid = existing_document["metadata"]["uid"]

    authored = controller.parse_authored_unit_document(authored_terraform("infra"), "infra")
    monkeypatch.setattr(controller, "load_environment_specifications", lambda *_args: {"infra": authored})
    monkeypatch.setattr(
        controller,
        "resolved_unit_source",
        lambda *_args: controller.ResolvedUnitSourceResult(
            source=controller.DesiredSource(
                path=".",
                revision="b" * 40,
                inputHash="sha256:inputs",
                driverVersion=controller.DRIVER_VERSIONS["terraform"],
            ),
            inputs_changed=False,
        ),
    )

    result = controller.build_desired_candidate(
        "dev",
        source_root,
        "b" * 40,
        current,
        observed,
        None,
        candidate,
        verbose=False,
    )

    retained = controller.load_desired_unit(candidate / "units/infra.json", "infra")
    orphan = controller.load_desired_unit(candidate / "units/orphan.json", "orphan")
    assert retained.metadata.uid == existing_uid
    assert orphan.metadata.lifecycle is not None
    assert orphan.metadata.lifecycle.management is not None
    assert "orphan" in result.cleanup_inputs

    repeated_candidate = tmp_path / "candidate-repeat"
    controller.build_desired_candidate(
        "dev",
        source_root,
        "b" * 40,
        current,
        observed,
        None,
        repeated_candidate,
        verbose=False,
    )
    assert controller.directory_files(candidate) == controller.directory_files(repeated_candidate)


def test_direct_same_name_resource_is_not_adopted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source_root = tmp_path / "source"
    current = tmp_path / "current"
    observed = tmp_path / "observed"
    candidate = tmp_path / "candidate"
    source_root.mkdir()
    (source_root / "gitopsctr.yaml").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Project",
                "metadata": {"name": "test"},
                "spec": {"effectLease": None},
            }
        )
    )
    current_units = current / "units"
    current_units.mkdir(parents=True)
    observed.mkdir()
    direct = legacy_desired("infra")
    direct.update(
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "KubernetesManifests",
            "metadata": {
                "name": "infra",
                "uid": "uid-direct",
                "lifecycle": {"management": {"mode": "direct"}},
            },
            "spec": {"source": {"path": ".", "revision": "a" * 40, "driverVersion": 2}},
        }
    )
    (current_units / "infra.json").write_text(json.dumps(direct))
    authored = controller.parse_authored_unit_document(authored_terraform("infra"), "infra")
    monkeypatch.setattr(controller, "load_environment_specifications", lambda *_args: {"infra": authored})
    monkeypatch.setattr(
        controller,
        "resolved_unit_source",
        lambda *_args: (
            controller.DesiredSource(path=".", revision="b" * 40, inputHash="sha256:inputs", driverVersion=2),
            False,
        ),
    )

    with pytest.raises(OperationError, match="directly managed"):
        controller.build_desired_candidate(
            "dev", source_root, "b" * 40, current, observed, None, candidate, verbose=False
        )
