from pathlib import Path

import pytest

from gitopsctr import controller
from gitopsctr.errors import OperationError
from gitopsctr.formats import Project
from gitopsctr.registry import RESOURCE_REGISTRY
from gitopsctr.resource_model import ResourcePlane
from tests.stack_deletion_support import stack_tree


def test_unit_qualified_names_use_local_metadata_and_hierarchical_storage(tmp_path: Path):
    root = tmp_path / "desired"
    stack_tree(root)
    resources = controller.load_desired_resource_graph(root)
    unit = next(resource for resource in resources.values() if isinstance(resource, controller.UnitResource))

    assert unit.name == "preview-app"
    assert (root / "units/preview/preview-app.json").is_file()
    assert controller.qualified_unit_name_map(resources) == {"preview/preview-app": "preview/preview-app"}
    assert controller.resolve_unit_selectors(resources, ("preview/preview-app",)) == ("preview/preview-app",)
    with pytest.raises(OperationError, match="unknown Unit qualified name"):
        controller.resolve_unit_selectors(resources, ("preview--preview-app",))


def test_registry_placement_encodes_hierarchical_collection_paths(tmp_path: Path):
    project = Project(name="test")
    common = {
        "root": tmp_path,
        "repository_root": tmp_path,
        "project": project,
        "environment": "dev",
        "suffix": ".yaml",
    }

    assert (
        RESOURCE_REGISTRY.document_path(
            family="unit",
            plane=ResourcePlane.DESIRED,
            qualified_name="application/image",
            **common,
        )
        == tmp_path / "units/application/image.yaml"
    )
    assert (
        RESOURCE_REGISTRY.document_path(
            family="receipt",
            plane=ResourcePlane.OBSERVED,
            qualified_name="application/image",
            **common,
        )
        == tmp_path / "units/application/image.yaml"
    )
    assert (
        RESOURCE_REGISTRY.document_path(
            family="artifact",
            plane=ResourcePlane.OBSERVED,
            qualified_name="application/image/containers",
            **common,
        )
        == tmp_path / "artifacts/application/image/containers.yaml"
    )


def test_finalized_unit_address_remains_selectable_for_cleanup(tmp_path: Path):
    root = tmp_path / "desired"
    stack_tree(root)
    resources = controller.load_desired_resource_graph(root)
    unit = next(resource for resource in resources.values() if isinstance(resource, controller.UnitResource))
    tombstone = controller.ResourceIncarnationTombstone(
        api_version=unit.gvk.api_version,
        kind=unit.gvk.kind,
        name=unit.name,
        uid=unit.metadata.uid or "",
        deletion_generation=1,
        qualified_name="preview/preview-app",
    )

    assert controller.resolve_unit_selectors({}, ("preview/preview-app",), (tombstone,)) == ("preview/preview-app",)


def test_nested_unit_cannot_reuse_its_finalized_uid(tmp_path: Path):
    root = tmp_path / "desired"
    stack_tree(root)
    resources = controller.load_desired_resource_graph(root)
    unit = next(resource for resource in resources.values() if isinstance(resource, controller.UnitResource))
    controller.write_resource_incarnation_tombstone(
        root,
        controller.ResourceIncarnationTombstone(
            api_version=unit.gvk.api_version,
            kind=unit.gvk.kind,
            name=unit.name,
            uid=unit.metadata.uid or "",
            deletion_generation=1,
            qualified_name="preview/preview-app",
        ),
    )

    with pytest.raises(OperationError, match="reuses finalized UID"):
        controller.load_desired_resource_graph(root)


def test_desired_graph_cache_reuses_only_content_identical_operation_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "desired"
    stack_tree(root)
    original = controller._load_desired_resource_graph
    loads = 0

    def counted_load(path: Path, *, validate: bool = True, **kwargs):
        nonlocal loads
        loads += 1
        return original(path, validate=validate, **kwargs)

    monkeypatch.setattr(controller, "_load_desired_resource_graph", counted_load)
    with controller._desired_graph_cache_scope():
        first = controller.load_desired_resource_graph(root, validate=False)
        first.clear()
        second = controller.load_desired_resource_graph(root, validate=False)
        assert second
        assert loads == 1

        controller.load_desired_resource_graph(root)
        assert loads == 1

        unit_path = next((root / "units").rglob("*.json"))
        unit_path.write_text(unit_path.read_text() + "\n")
        controller.load_desired_resource_graph(root)
        assert loads == 2

    with controller._desired_graph_cache_scope():
        controller.load_desired_resource_graph(root)
        assert loads == 3


def test_desired_graph_cache_does_not_remember_failed_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "desired"
    stack_tree(root)
    unit_path = next((root / "units").rglob("*.json"))
    unit_path.write_text("not json")
    original = controller._load_desired_resource_graph
    loads = 0

    def counted_load(path: Path, *, validate: bool = True, **kwargs):
        nonlocal loads
        loads += 1
        return original(path, validate=validate, **kwargs)

    monkeypatch.setattr(controller, "_load_desired_resource_graph", counted_load)
    with controller._desired_graph_cache_scope():
        for _ in range(2):
            with pytest.raises(OperationError):
                controller.load_desired_resource_graph(root)
    assert loads == 2


def test_effect_lease_and_tombstone_round_trip_qualified_name(tmp_path: Path):
    lease = controller.EffectLease(
        unit_name="image",
        uid="d1-image",
        token="lease-token",
        owner="runner",
        desired_revision="a" * 40,
        qualified_name="application/image",
    )
    restored_lease = controller.EffectLease.from_document(lease.document(), "application/image")
    assert restored_lease.qualified_name == "application/image"

    tombstone = controller.ResourceIncarnationTombstone(
        api_version=controller.UNIT_API_VERSION,
        kind="OciImages",
        name="image",
        uid="d1-image",
        deletion_generation=2,
        qualified_name="application/image",
    )
    restored_tombstone = controller.ResourceIncarnationTombstone.from_document(tombstone.document())
    assert restored_tombstone.qualified_name == "application/image"
    assert controller.write_resource_incarnation_tombstone(tmp_path, tombstone) == (
        tmp_path
        / controller.DESIRED_RESOURCE_INCARNATIONS_PATH
        / "unit.gitopsctr.io/v1/OciImages/application/image/d1-image.json"
    )
