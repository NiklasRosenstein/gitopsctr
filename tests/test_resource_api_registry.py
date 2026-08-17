from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from gitopsctr.resource_api import (
    GVK,
    ApiKind,
    JsonFieldPath,
    PersistedQualifiedNameAddressing,
    ResourceApiContribution,
    ResourceApiError,
    ResourceApiFamily,
    ResourceApiRelationship,
    RootResourceAddressing,
    build_resource_api_registry,
)


@dataclass(frozen=True)
class ExactKinds:
    gvks: frozenset[GVK]

    def matches(self, api_kind: ApiKind[object]) -> bool:
        return api_kind.gvk in self.gvks


WIDGET = ApiKind(GVK("example.test/v1", "Widget"), object())
PART = ApiKind(GVK("example.test/v1", "Part"), object())
STATUS = ApiKind(GVK("example.test/v1", "Status"), object())


def family(name: str, api_kind: ApiKind[object], *, addressing=None) -> ResourceApiFamily:
    return ResourceApiFamily(
        name,
        name,
        f"{name}s",
        (ExactKinds(frozenset((api_kind.gvk,))),),
        addressing=addressing or RootResourceAddressing(),
    )


def synthetic_registry():
    widget = family("widget", WIDGET)
    part = family(
        "part",
        PART,
        addressing=PersistedQualifiedNameAddressing(
            JsonFieldPath(("owner",)), "widget", "widget-has-part", append_local_name=True
        ),
    )
    status = family(
        "status",
        STATUS,
        addressing=PersistedQualifiedNameAddressing(JsonFieldPath(("subject",)), "widget", "status-observes-widget"),
    )
    relationships = (
        ResourceApiRelationship("widget-has-part", "widget", "part", "part", "widget"),
        ResourceApiRelationship("status-observes-widget", "status", "widget", "status", "widget"),
    )
    return build_resource_api_registry((WIDGET, PART, STATUS), (widget, part, status), relationships)


def test_synthetic_catalog_covers_root_child_and_mirror_addresses_without_gitopsctr_domain():
    registry = synthetic_registry()

    assert registry.family("widgets").name == "widget"
    assert registry.family_for_api_kind(PART.gvk).name == "part"
    assert registry.api_kinds_for_family("status") == (STATUS,)
    assert registry.family("widget").identity.render(registry.family("widget").identity.parse("sample")) == "sample"

    part = registry.family("part")
    part_record = SimpleNamespace(name="wheel", document={"owner": "vehicle"})
    assert part.addressing.qualified_name(part_record, SimpleNamespace()) == "vehicle/wheel"

    status = registry.family("status")
    status_record = SimpleNamespace(name="ready", document={"subject": "vehicle"})
    assert status.addressing.qualified_name(status_record, SimpleNamespace()) == "vehicle"


def test_contributions_merge_and_reject_gvk_and_selector_collisions():
    registry = synthetic_registry()
    note = ApiKind(GVK("example.test/v1", "Note"), object())
    contribution = ResourceApiContribution(api_kinds=(note,), families=(family("note", note),))

    merged = build_resource_api_registry(
        registry.api_kinds,
        registry.families,
        registry.relationships,
        (contribution,),
    )
    assert merged.family_for_api_kind(note.gvk).name == "note"

    with pytest.raises(ResourceApiError, match="duplicate API kind registration"):
        build_resource_api_registry(
            registry.api_kinds,
            registry.families,
            registry.relationships,
            (ResourceApiContribution(api_kinds=(WIDGET,)),),
        )

    with pytest.raises(ResourceApiError, match="duplicate resource selector 'widgets'"):
        build_resource_api_registry(
            (*registry.api_kinds.values(), note),
            (*registry.families, replace(family("note", note), aliases=("widgets",))),
            registry.relationships,
        )


def test_registry_rejects_missing_endpoints_ambiguous_membership_and_address_cycles():
    registry = synthetic_registry()
    with pytest.raises(ResourceApiError, match="references an unknown family: missing"):
        build_resource_api_registry(
            registry.api_kinds,
            registry.families,
            (*registry.relationships, ResourceApiRelationship("missing-edge", "widget", "missing")),
        )

    overlapping = replace(
        registry.family("status"), membership_rules=(ExactKinds(frozenset((STATUS.gvk, WIDGET.gvk))),)
    )
    with pytest.raises(ResourceApiError, match="Widget matches 2 family membership rules"):
        build_resource_api_registry(
            registry.api_kinds,
            tuple(overlapping if item.name == "status" else item for item in registry.families),
            registry.relationships,
        )

    widget = replace(
        registry.family("widget"),
        addressing=PersistedQualifiedNameAddressing(JsonFieldPath(("part",)), "part", "widget-belongs-part"),
    )
    with pytest.raises(ResourceApiError, match="addressing relationships must be acyclic"):
        build_resource_api_registry(
            registry.api_kinds,
            tuple(widget if item.name == "widget" else item for item in registry.families),
            (
                *registry.relationships,
                ResourceApiRelationship("widget-belongs-part", "widget", "part", "widget", "part"),
            ),
        )
