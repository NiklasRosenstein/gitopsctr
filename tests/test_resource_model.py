from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import cast

import pytest
import yaml

from gitopsctr.api import GVK, ApiKind
from gitopsctr.artifacts import CONTAINER_IMAGES
from gitopsctr.contracts import (
    DesiredStackSpec,
    DesiredStackTemplateSpec,
    StackProjection,
    StackProjectionUnit,
    StackTemplateAcquisition,
    StackTemplateFromInput,
    StackTemplateReference,
    StackTemplateRequestedFromInput,
    StackTemplateResolvedFromInput,
    StackTemplateSpec,
    StackTemplateUnitTemplate,
)
from gitopsctr.core_api import CORE_API_VERSION, CoreResourceApi
from gitopsctr.document import ContractError, JsonObject, JsonObjectValue
from gitopsctr.driver import UnitDriver
from gitopsctr.formats import Project
from gitopsctr.inspection_api import InspectionOutputApi
from gitopsctr.registry import API_KINDS, RESOURCE_REGISTRY
from gitopsctr.resource_model import (
    ArtifactResolutionContext,
    CollectionReadContext,
    IdentityConstraint,
    ObservationCardinality,
    ObservationState,
    ProfiledApiMembership,
    ReceiptArtifactDescriptionBinding,
    ReceiptObservationBinding,
    RelationshipResource,
    ResourceIdentity,
    ResourceModelError,
    ResourcePlane,
    ResourceRegistry,
    ResourceScope,
    ResourceSelection,
    UnitApiMembership,
)
from gitopsctr.resource_model_export import bundled_resource_registry, export_resource_model, render_resource_model
from gitopsctr.resources import ResourceMetadata, StackResource, UnitResource
from gitopsctr.templates import TemplateObject


def rebuild(
    *, collections=None, families=None, observations=None, artifact_descriptions=None, graph_relationships=None
) -> ResourceRegistry:
    return ResourceRegistry(
        RESOURCE_REGISTRY.api_kinds,
        RESOURCE_REGISTRY.collections if collections is None else collections,
        RESOURCE_REGISTRY.families if families is None else families,
        RESOURCE_REGISTRY.observations if observations is None else observations,
        RESOURCE_REGISTRY.artifact_descriptions if artifact_descriptions is None else artifact_descriptions,
        RESOURCE_REGISTRY.graph_relationships if graph_relationships is None else graph_relationships,
    )


def replace_family(name: str, **changes):
    return tuple(replace(family, **changes) if family.name == name else family for family in RESOURCE_REGISTRY.families)


def relationship_resource(
    api_version: str,
    kind: str,
    name: str,
    document: JsonObject,
    *,
    path: str,
    blob_id: str | None = None,
    content_digest: str | None = None,
    media_type: str | None = None,
) -> RelationshipResource:
    return RelationshipResource(
        ResourceIdentity(api_version, kind, name),
        document,
        document,
        PurePosixPath(path),
        blob_id,
        content_digest,
        media_type,
    )


def test_builtin_registry_derives_core_driver_and_artifact_family_membership():
    assert len(API_KINDS) == 14
    assert isinstance(API_KINDS[GVK("inspection.gitopsctr.io/v1", "ResourceList")].spec, InspectionOutputApi)
    assert {RESOURCE_REGISTRY.family_for_api_kind(gvk).name for gvk in RESOURCE_REGISTRY.api_kinds} == {
        "artifact",
        "environment",
        "project",
        "promotion",
        "receipt",
        "stack",
        "stacktemplate",
        "unit",
    }
    assert {str(kind.gvk) for kind in RESOURCE_REGISTRY.api_kinds_for_family("stacktemplates")} == {
        "gitopsctr.io/v1/StackTemplate"
    }
    assert len(RESOURCE_REGISTRY.api_kinds_for_family("units")) == 5
    assert len(RESOURCE_REGISTRY.api_kinds_for_family("artifacts")) == 2

    unit = RESOURCE_REGISTRY.family("unit")
    assert {(item.plane, item.scope, item.contract_profile) for item in unit.placements} == {
        (ResourcePlane.SOURCE, ResourceScope.ENVIRONMENT, "authored"),
        (ResourcePlane.DESIRED, ResourceScope.ENVIRONMENT, "desired"),
    }
    assert next(item for item in unit.placements if item.default_for_inspection).plane is ResourcePlane.DESIRED
    assert set(RESOURCE_REGISTRY.contracts_for("unit", "desired")) == {
        kind.gvk for kind in RESOURCE_REGISTRY.api_kinds_for_family("unit")
    }
    assert {relationship.name for relationship in RESOURCE_REGISTRY.graph_relationships} == {
        "stack-selects-stacktemplate",
        "stack-owns-unit",
    }


def test_family_local_identity_is_registry_defined_and_generically_selectable():
    artifact = RESOURCE_REGISTRY.family("artifact")
    unit = RESOURCE_REGISTRY.family("unit")

    identity = artifact.identity.parse("application--image/containers")
    assert artifact.identity.render(identity) == "application--image/containers"
    assert artifact.identity.value(identity, "producer") == "application--image"
    assert artifact.identity.value(identity, "name") == "containers"
    assert artifact.identity.matches(
        identity,
        ResourceSelection(constraints=(IdentityConstraint("producer", frozenset(("application--image",))),)),
    )
    assert not artifact.identity.matches(
        identity,
        ResourceSelection(constraints=(IdentityConstraint("producer", frozenset(("other",))),)),
    )
    assert artifact.identity.segments[0].filter_option == "--producer"
    with pytest.raises(ResourceModelError, match="requires 1 segments"):
        unit.identity.parse("application--image/containers")


def test_registered_stack_template_selection_binding_checks_uid_and_content_digest():
    unit_templates = {
        "app": StackTemplateUnitTemplate(apiVersion="unit.gitopsctr.io/v1", kind="Terraform", spec=TemplateObject({}))
    }
    content = StackTemplateSpec(parameters=[], unitTemplates=unit_templates)
    template_spec = DesiredStackTemplateSpec(
        parameters=[],
        contentDigest=content.semantic_content_digest(),
        acquisition=StackTemplateAcquisition(
            documentDigest="sha256:" + "b" * 64,
            requestedSource=StackTemplateRequestedFromInput(fromInput=StackTemplateFromInput()),
            resolvedSource=StackTemplateResolvedFromInput(fromInput=StackTemplateFromInput()),
        ),
        unitTemplates=unit_templates,
    )
    template = StackResource(
        GVK(CORE_API_VERSION, "StackTemplate"),
        ResourceMetadata(name="preview", uid="template-uid"),
        template_spec,
    )
    stack = StackResource(
        GVK(CORE_API_VERSION, "Stack"),
        ResourceMetadata(name="preview", uid="stack-uid"),
        DesiredStackSpec(
            templateRef=StackTemplateReference(
                name="preview",
                uid="template-uid",
                contentDigest=template_spec.semantic_content_digest(),
            ),
            structuralProjection=StackProjection.build(
                stack_uid="stack-uid",
                template_uid="template-uid",
                template_content_digest=template_spec.contentDigest,
                context_digest="sha256:" + "c" * 64,
                units={
                    "app": StackProjectionUnit(
                        apiVersion="unit.gitopsctr.io/v1",
                        kind="Terraform",
                        spec=JsonObjectValue({}),
                        dependsOn=[],
                    )
                },
            ),
        ),
    )
    binding = RESOURCE_REGISTRY.graph_relationship("stack-selects-stacktemplate").binding
    binding.validate(stack, template)
    with pytest.raises(ResourceModelError, match="different UID"):
        binding.validate(
            stack,
            replace(template, metadata=replace(template.metadata, uid="other-template-uid")),
        )
    other_unit_templates = {
        "other": StackTemplateUnitTemplate(apiVersion="unit.gitopsctr.io/v1", kind="Terraform", spec=TemplateObject({}))
    }
    other_content = StackTemplateSpec(parameters=[], unitTemplates=other_unit_templates)
    other_template = replace(
        template,
        spec=DesiredStackTemplateSpec(
            parameters=[],
            contentDigest=other_content.semantic_content_digest(),
            acquisition=template_spec.acquisition,
            unitTemplates=other_unit_templates,
        ),
    )
    with pytest.raises(ResourceModelError, match="different content digest"):
        binding.validate(stack, other_template)


def test_core_api_profiles_are_executable_resource_contracts():
    expected = {
        "Project": {"authored"},
        "Environment": {"authored"},
        "StackTemplate": {"authored", "desired"},
        "Stack": {"authored", "desired"},
        "Promotion": {"desired"},
        "Receipt": set(),
    }
    for kind, profiles in expected.items():
        gvk = GVK(CORE_API_VERSION, kind)
        assert isinstance(API_KINDS[gvk].spec, CoreResourceApi)
        assert set(cast(CoreResourceApi, API_KINDS[gvk].spec).profiles) == profiles
        assert all(RESOURCE_REGISTRY.contract(gvk, profile) is not None for profile in profiles)

    environment = {
        "apiVersion": CORE_API_VERSION,
        "kind": "Environment",
        "metadata": {"name": "dev"},
        "spec": {},
    }
    contract = RESOURCE_REGISTRY.contract(GVK(CORE_API_VERSION, "Environment"), "authored")
    dumped = contract.dump(contract.parse(environment))
    assert dumped["apiVersion"] == CORE_API_VERSION
    assert dumped["kind"] == "Environment"
    assert cast(dict, dumped["metadata"])["name"] == "dev"

    for invalid in (
        {**environment, "garbage": True},
        {**environment, "metadata": {"name": "dev", "garbage": True}},
        {**environment, "metadata": {"name": ""}},
    ):
        with pytest.raises(ContractError):
            contract.parse(invalid)

    promotion = {
        "apiVersion": CORE_API_VERSION,
        "kind": "Promotion",
        "metadata": {"name": "dev"},
        "spec": {
            "source": {
                "environment": "dev",
                "desiredRef": "desired/dev",
                "desiredRevision": "a" * 40,
                "observedRef": "observed/dev",
                "observedRevision": None,
            },
            "specificationRevision": "b" * 40,
        },
    }
    promotion_contract = RESOURCE_REGISTRY.contract(GVK(CORE_API_VERSION, "Promotion"), "desired")
    promotion_contract.parse(promotion)
    with pytest.raises(ContractError, match="must match"):
        promotion_contract.parse({**promotion, "metadata": {"name": "staging"}})


def test_receipt_contract_dispatches_to_subject_driver_result_and_artifacts():
    document = {
        "apiVersion": CORE_API_VERSION,
        "kind": "Receipt",
        "metadata": {"name": "infrastructure"},
        "spec": {
            "subject": {
                "apiVersion": "unit.gitopsctr.io/v1",
                "kind": "Terraform",
                "name": "infrastructure",
            },
            "desired": {"unitBlob": "blob"},
        },
        "status": {
            "controller": {},
            "result": {"applied": {"sourceRevision": "a" * 40}, "outputs": {}},
        },
    }
    contract = RESOURCE_REGISTRY.contract(GVK(CORE_API_VERSION, "Receipt"), "observed")
    parsed = contract.parse(document)
    assert parsed.name == "infrastructure"
    with pytest.raises(ContractError, match="invalid terraform receipt"):
        contract.parse({**document, "status": {"controller": {}, "result": {}}})


def test_collection_provider_discovers_and_parses_registered_family(tmp_path: Path):
    path = tmp_path / "deployment/environments/dev/environment.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("apiVersion: gitopsctr.io/v1\nkind: Environment\nmetadata:\n  name: dev\nspec: {}\n")
    family = RESOURCE_REGISTRY.family("environment")
    placement = family.placements[0]
    collection = RESOURCE_REGISTRY.collection(placement.collection)
    context = CollectionReadContext(
        root=tmp_path,
        repository_root=tmp_path,
        project=Project(name="test"),
        environment="dev",
        family=family,
        placement=placement,
        api_kinds=RESOURCE_REGISTRY.api_kinds,
        contracts=RESOURCE_REGISTRY.contracts_for(family.name, placement.contract_profile),
        blob_ids={PurePosixPath("deployment/environments/dev/environment.yaml"): "blob-1"},
    )
    resources = tuple(collection.provider.discover(context))
    assert len(resources) == 1
    assert resources[0].name == "dev"
    assert resources[0].gvk == GVK(CORE_API_VERSION, "Environment")
    assert resources[0].blob_id == "blob-1"
    assert resources[0].content_digest.startswith("sha256:")


@pytest.mark.parametrize("profile", ["authored", "desired"])
def test_collection_provider_discovers_full_unit_resource_envelopes(tmp_path: Path, profile: str):
    document = {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "Terraform",
        "metadata": {"name": "infrastructure"},
        "spec": {"source": {"path": "."}},
    }
    family = RESOURCE_REGISTRY.family("unit")
    placement = next(item for item in family.placements if item.contract_profile == profile)
    if profile == "authored":
        root = tmp_path
        path = root / "deployment/environments/dev/units/infrastructure.yaml"
    else:
        root = tmp_path / "desired"
        path = root / "units/infrastructure.yaml"
        document["metadata"] = {
            "name": "infrastructure",
            "uid": "infrastructure",
            "labels": {"gitopsctr.io/partition": "application"},
        }
        cast(dict, document["spec"])["source"] = {
            "path": ".",
            "revision": "a" * 40,
            "driverVersion": 2,
        }
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    collection = RESOURCE_REGISTRY.collection(placement.collection)
    context = CollectionReadContext(
        root=root,
        repository_root=tmp_path,
        project=Project(name="test"),
        environment="dev",
        family=family,
        placement=placement,
        api_kinds=RESOURCE_REGISTRY.api_kinds,
        contracts=RESOURCE_REGISTRY.contracts_for(family.name, profile),
    )

    resources = tuple(collection.provider.discover(context))

    assert len(resources) == 1
    assert resources[0].gvk == GVK("unit.gitopsctr.io/v1", "Terraform")
    assert resources[0].name == "infrastructure"
    assert isinstance(resources[0].parsed, UnitResource)
    assert resources[0].parsed.name == "infrastructure"
    contract = RESOURCE_REGISTRY.contract(resources[0].gvk, profile)
    dumped = contract.dump(resources[0].parsed)
    assert dumped["apiVersion"] == document["apiVersion"]
    assert dumped["kind"] == document["kind"]
    assert contract.parse(dumped).name == "infrastructure"


def test_desired_unit_contract_rejects_metadata_without_canonical_identity():
    contract = RESOURCE_REGISTRY.contract(GVK("unit.gitopsctr.io/v1", "Terraform"), "desired")
    with pytest.raises(ContractError, match="uid"):
        contract.parse(
            {
                "apiVersion": "unit.gitopsctr.io/v1",
                "kind": "Terraform",
                "metadata": {"name": "infrastructure"},
                "spec": {"source": {"path": ".", "revision": "a" * 40, "driverVersion": 2}},
            }
        )


def test_receipt_observation_binding_executes_identity_and_freshness():
    definition = RESOURCE_REGISTRY.observations[0]
    assert isinstance(definition.binding, ReceiptObservationBinding)
    receipt = relationship_resource(
        CORE_API_VERSION,
        "Receipt",
        "application",
        {
            "apiVersion": CORE_API_VERSION,
            "kind": "Receipt",
            "metadata": {"name": "application"},
            "spec": {
                "subject": {"apiVersion": "unit.gitopsctr.io/v1", "kind": "Terraform", "name": "application"},
                "desired": {"unitBlob": "blob-a"},
            },
            "status": {"controller": {}, "result": {}},
        },
        path="units/application.yaml",
    )
    unit = relationship_resource(
        "unit.gitopsctr.io/v1",
        "Terraform",
        "application",
        {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "Terraform",
            "metadata": {"name": "application"},
            "spec": {},
        },
        path="units/application.yaml",
        blob_id="blob-a",
    )
    assert definition.binding.subject_identity(receipt) == unit.identity
    assert definition.binding.evaluate(receipt, unit) is ObservationState.CURRENT
    assert definition.binding.evaluate(receipt, replace(unit, blob_id="blob-b")) is ObservationState.STALE
    mismatched = replace(receipt, identity=ResourceIdentity(CORE_API_VERSION, "Receipt", "another-name"))
    with pytest.raises(ResourceModelError, match="subject name must match"):
        definition.binding.subject_identity(mismatched)


def test_receipt_artifact_binding_executes_identity_digest_media_and_producer_invariants():
    definition = RESOURCE_REGISTRY.artifact_descriptions[0]
    assert isinstance(definition.binding, ReceiptArtifactDescriptionBinding)
    artifact_path = PurePosixPath("artifacts/application/containers.yaml")
    producer = relationship_resource(
        "unit.gitopsctr.io/v1",
        "OciImages",
        "application",
        {"apiVersion": "unit.gitopsctr.io/v1", "kind": "OciImages", "metadata": {"name": "application"}, "spec": {}},
        path="units/application.yaml",
    )
    artifact = relationship_resource(
        "artifact.gitopsctr.io/v1",
        "ContainerImages",
        "containers",
        {
            "apiVersion": "artifact.gitopsctr.io/v1",
            "kind": "ContainerImages",
            "metadata": {"name": "containers"},
            "producer": {"apiVersion": "unit.gitopsctr.io/v1", "kind": "OciImages", "name": "application"},
            "images": {},
        },
        path=str(artifact_path),
        content_digest="sha256:" + "a" * 64,
        media_type="application/vnd.gitopsctr.container-images.v1+yaml",
    )
    receipt = relationship_resource(
        CORE_API_VERSION,
        "Receipt",
        "application",
        {
            "apiVersion": CORE_API_VERSION,
            "kind": "Receipt",
            "metadata": {"name": "application"},
            "spec": {
                "subject": {"apiVersion": "unit.gitopsctr.io/v1", "kind": "OciImages", "name": "application"},
                "desired": {"unitBlob": "blob"},
            },
            "status": {
                "controller": {},
                "result": {},
                "artifacts": {
                    "containers": {
                        "apiVersion": "artifact.gitopsctr.io/v1",
                        "kind": "ContainerImages",
                        "path": str(artifact_path),
                        "digest": "sha256:" + "a" * 64,
                        "mediaType": "application/vnd.gitopsctr.container-images.v1+yaml",
                    }
                },
            },
        },
        path="units/application.yaml",
    )
    context = ArtifactResolutionContext(
        producer,
        {artifact_path: artifact},
        {"containers": GVK("artifact.gitopsctr.io/v1", "ContainerImages")},
        ObservationState.CURRENT,
    )
    links = definition.binding.resolve(receipt, context)
    assert links[0].name == "containers"
    assert links[0].artifact is artifact
    with pytest.raises(ResourceModelError, match="wrong digest"):
        definition.binding.resolve(
            receipt,
            replace(context, artifacts_by_path={artifact_path: replace(artifact, content_digest="wrong")}),
        )

    desired_unit = {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "OciImages",
        "metadata": {
            "name": "application",
            "uid": "application",
            "labels": {"gitopsctr.io/partition": "application"},
        },
        "spec": {
            "source": {
                "path": ".",
                "revision": "b" * 40,
                "driverVersion": 7,
                "inputHash": "sha256:inputs",
            }
        },
    }
    typed_producer = RESOURCE_REGISTRY.contract(GVK("unit.gitopsctr.io/v1", "OciImages"), "desired").parse(desired_unit)
    pinned_artifact_document = {
        **artifact.document,
        "producer": {
            "apiVersion": "unit.gitopsctr.io/v1",
            "kind": "OciImages",
            "name": "application",
            "driverVersion": 7,
            "sourceRevision": "b" * 40,
            "inputHashVersion": 1,
            "inputHash": "sha256:inputs",
        },
    }
    pinned_artifact = replace(artifact, document=pinned_artifact_document)
    pinned_context = replace(
        context,
        producer=replace(producer, document=desired_unit, parsed=typed_producer),
        artifacts_by_path={artifact_path: pinned_artifact},
    )
    definition.binding.resolve(receipt, pinned_context)
    stale_document = {
        **pinned_artifact_document,
        "producer": {**cast(dict, pinned_artifact_document["producer"]), "sourceRevision": "c" * 40},
    }
    with pytest.raises(ResourceModelError, match="stale producer source pin"):
        definition.binding.resolve(
            receipt,
            replace(
                pinned_context,
                artifacts_by_path={artifact_path: replace(pinned_artifact, document=stale_document)},
            ),
        )


def test_registry_rejects_duplicate_selectors_and_aliases():
    with pytest.raises(ResourceModelError, match="duplicate resource selector 'units'"):
        rebuild(families=replace_family("artifact", aliases=("units",)))


@pytest.mark.parametrize(
    ("placement_changes", "message"),
    [
        ({"contract_profile": "observed"}, "profile 'observed' is not supported"),
        ({"collection": "source-environments"}, "placement is incompatible"),
        ({"collection": "not-installed"}, "references unknown collection"),
    ],
)
def test_registry_rejects_invalid_placements(placement_changes, message):
    unit = RESOURCE_REGISTRY.family("unit")
    authored, desired = unit.placements
    with pytest.raises(ResourceModelError, match=message):
        rebuild(families=replace_family("unit", placements=(replace(authored, **placement_changes), desired)))


def test_registry_rejects_ambiguous_inspection_default():
    unit = RESOURCE_REGISTRY.family("unit")
    families = replace_family(
        "unit", placements=tuple(replace(placement, default_for_inspection=True) for placement in unit.placements)
    )
    with pytest.raises(ResourceModelError, match="exactly one default placement"):
        rebuild(families=families)

    assert unit.inspection is not None
    families = replace_family("unit", inspection=replace(unit.inspection, default_plane=ResourcePlane.SOURCE))
    with pytest.raises(ResourceModelError, match="default plane does not match"):
        rebuild(families=families)


def test_registry_rejects_zero_or_multiple_family_membership():
    project_rule = ProfiledApiMembership(frozenset((GVK(CORE_API_VERSION, "Missing"),)), CoreResourceApi)
    with pytest.raises(ResourceModelError, match="Project.*matches 0 family membership rules"):
        rebuild(families=replace_family("project", membership_rules=(project_rule,)))

    artifact = RESOURCE_REGISTRY.family("artifact")
    duplicate_rule = UnitApiMembership(UnitDriver)
    with pytest.raises(ResourceModelError, match="matches 2 family membership rules"):
        rebuild(families=replace_family("artifact", membership_rules=(*artifact.membership_rules, duplicate_rule)))


def test_registry_rejects_missing_observation_endpoint_and_non_executable_bindings():
    invalid = replace(RESOURCE_REGISTRY.observations[0], subject_family="deployment")
    with pytest.raises(ResourceModelError, match="references an unknown family"):
        rebuild(observations=(invalid,))
    invalid = replace(RESOURCE_REGISTRY.observations[0], binding=cast(ReceiptObservationBinding, object()))
    with pytest.raises(ResourceModelError, match="no executable binding"):
        rebuild(observations=(invalid,))


def test_registry_rejects_invalid_observation_cardinality():
    invalid = replace(RESOURCE_REGISTRY.observations[0], cardinality=cast(ObservationCardinality, "many"))
    with pytest.raises(ResourceModelError, match="unsupported cardinality"):
        rebuild(observations=(invalid,))


def test_registry_rejects_unplaced_artifact_relationship_planes():
    invalid = replace(RESOURCE_REGISTRY.artifact_descriptions[0], producer_plane=ResourcePlane.OBSERVED)
    with pytest.raises(ResourceModelError, match="producer plane is not placed"):
        rebuild(artifact_descriptions=(invalid,))


def test_registry_rejects_non_authoritative_driver_artifact_output(monkeypatch):
    driver_api = next(kind for kind in API_KINDS.values() if kind.gvk.kind == "OciImages")
    duplicate = ApiKind(CONTAINER_IMAGES.gvk, CONTAINER_IMAGES.spec)
    monkeypatch.setattr(type(driver_api.spec), "artifact_outputs", {"containers": duplicate})
    with pytest.raises(ResourceModelError, match="is not an authoritative registration"):
        rebuild()


def test_registry_validates_inspection_relationship_references_and_topology():
    unit = RESOURCE_REGISTRY.family("unit")
    assert unit.inspection is not None
    unknown = replace(unit.inspection, observation="missing-observation")
    with pytest.raises(ResourceModelError, match="unknown observation"):
        rebuild(families=replace_family("unit", inspection=unknown))

    stack_observation = replace(
        RESOURCE_REGISTRY.observations[0],
        name="promotion-observes-stack",
        observer_family="promotion",
        observer_plane=ResourcePlane.DESIRED,
        subject_family="stack",
        subject_plane=ResourcePlane.DESIRED,
    )
    mismatched = replace(unit.inspection, observation=stack_observation.name)
    with pytest.raises(ResourceModelError, match="does not include its default representation"):
        rebuild(
            families=replace_family("unit", inspection=mismatched),
            observations=(*RESOURCE_REGISTRY.observations, stack_observation),
        )


def test_registry_rejects_inspectable_family_without_executable_presenter():
    unit = RESOURCE_REGISTRY.family("unit")
    assert unit.inspection is not None
    with pytest.raises(ResourceModelError, match="no executable presenter"):
        rebuild(families=replace_family("unit", inspection=replace(unit.inspection, presenter=object())))


def test_generated_resource_model_is_deterministic_and_checked_in(tmp_path: Path):
    destination = tmp_path / "resource-model.md"
    export_resource_model(destination, check=False)
    assert destination.read_text() == render_resource_model(RESOURCE_REGISTRY)
    export_resource_model(destination, check=True)
    destination.write_text(destination.read_text() + "stale\n")
    with pytest.raises(RuntimeError, match="generated resource model is stale"):
        export_resource_model(destination, check=True)
    checked_in = Path(__file__).parents[1] / "docs" / "resource-model.md"
    assert checked_in.read_text() == render_resource_model(RESOURCE_REGISTRY)
    assert set(bundled_resource_registry().api_kinds) == set(RESOURCE_REGISTRY.api_kinds)
