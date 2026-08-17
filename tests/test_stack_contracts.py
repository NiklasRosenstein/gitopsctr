from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from gitopsctr import schemas
from gitopsctr.contracts import (
    CORE_CONTRACTS,
    AuthoredResourceMetadata,
    DesiredResourceMetadata,
    DesiredStackDocument,
    DesiredStackSpec,
    DesiredStackTemplateDocument,
    DesiredStackTemplateSpec,
    MashumaroContract,
    ResolvedArtifactImport,
    StackActiveProjection,
    StackDocument,
    StackProjection,
    StackProjectionUnit,
    StackProjectionUnitBinding,
    StackSpec,
    StackTemplateAcquisition,
    StackTemplateFromInput,
    StackTemplateGitRequest,
    StackTemplatePromotionRequest,
    StackTemplateReference,
    StackTemplateRequestedFromGit,
    StackTemplateRequestedFromInput,
    StackTemplateRequestedFromPromotion,
    StackTemplateResolvedFromGit,
    StackTemplateResolvedFromGitSource,
    StackTemplateResolvedFromInput,
    StackTemplateResolvedFromPromotion,
    StackTemplateResolvedFromPromotionSource,
    StackTemplateSourceContext,
)
from gitopsctr.document import ContractError, JsonObjectValue
from gitopsctr.templates import (
    ProjectionObject,
    PromotionReference,
    TemplateError,
    dump_template_value,
    parse_projection_value,
)

TEST_PROJECTION_CONTEXT_DIGEST = "sha256:" + "c" * 64


def template_document(resources: list[dict[str, object]], parameters: list[dict[str, str]]) -> dict[str, object]:
    unit_templates = {
        str(resource["name"]): {key: value for key, value in resource.items() if key != "name"}
        for resource in resources
    }
    return {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "StackTemplate",
        "metadata": {"name": "preview"},
        "spec": {"parameters": parameters, "unitTemplates": unit_templates},
    }


def unit_resource(name: str, spec: dict[str, object], *, kind: str = "Terraform", **extra: object) -> dict[str, object]:
    return {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": kind,
        "name": name,
        "spec": spec,
        **extra,
    }


def desired_template_document(authored: dict[str, object]) -> dict[str, object]:
    authored_spec = authored["spec"]
    assert isinstance(authored_spec, dict)
    typed = CORE_CONTRACTS["stack-template-authored"].parse(authored)
    digest = typed.spec.semantic_content_digest()
    return {
        **authored,
        "metadata": {"name": "preview", "uid": "template-uid"},
        "spec": {
            **authored_spec,
            "contentDigest": digest,
            "acquisition": {
                "documentDigest": "sha256:" + "b" * 64,
                "requestedSource": {"fromInput": {}},
                "resolvedSource": {"fromInput": {}},
            },
        },
    }


def test_direct_inline_template_and_stack_contracts_are_typed_and_round_trip():
    authored = template_document(
        [unit_resource("app", {"value": "fixed"})],
        [{"name": "name", "type": "string"}],
    )
    parsed = CORE_CONTRACTS["stack-template-authored"].parse(authored)
    assert parsed.metadata == AuthoredResourceMetadata(name="preview")

    desired = CORE_CONTRACTS["stack-template-desired"].parse(desired_template_document(authored))
    assert isinstance(desired, DesiredStackTemplateDocument)
    assert desired.spec.sourceContext is None
    assert isinstance(desired.spec.acquisition.requestedSource.fromInput, StackTemplateFromInput)
    assert isinstance(desired.spec.acquisition.resolvedSource.fromInput, StackTemplateFromInput)
    assert desired.spec.contentDigest == desired.spec.semantic_content_digest()

    stack = StackDocument(
        apiVersion="gitopsctr.io/v1",
        kind="Stack",
        metadata=AuthoredResourceMetadata(name="web"),
        spec=StackSpec(template="preview", parameters=JsonObjectValue({"name": "web"})),
    )
    assert CORE_CONTRACTS["stack-authored"].parse(CORE_CONTRACTS["stack-authored"].dump(stack)) == stack

    desired_stack = DesiredStackDocument(
        apiVersion="gitopsctr.io/v1",
        kind="Stack",
        metadata=DesiredResourceMetadata(name="web", uid="stack-uid"),
        spec=DesiredStackSpec(
            templateRef=StackTemplateReference(
                name="preview", uid="template-uid", contentDigest=desired.spec.contentDigest
            ),
            structuralProjection=StackProjection.build(
                stack_uid="stack-uid",
                template_uid="template-uid",
                template_content_digest=desired.spec.contentDigest,
                context_digest=TEST_PROJECTION_CONTEXT_DIGEST,
                units={},
            ),
        ),
    )
    assert CORE_CONTRACTS["stack-desired"].parse(CORE_CONTRACTS["stack-desired"].dump(desired_stack)) == desired_stack

    with pytest.raises(ValueError, match="templateRef UID"):
        replace(
            desired_stack.spec,
            templateRef=replace(desired_stack.spec.templateRef, uid="other-template"),
        )
    with pytest.raises(ValueError, match="metadata.uid"):
        DesiredStackDocument(
            apiVersion="gitopsctr.io/v1",
            kind="Stack",
            metadata=DesiredResourceMetadata(name="web", uid="other-stack"),
            spec=desired_stack.spec,
        )
    with pytest.raises(ValueError, match="invalid Stack template name"):
        StackSpec(template="Not a name")
    with pytest.raises(ValueError, match="dependencies"):
        StackProjectionUnit(
            apiVersion="unit.gitopsctr.io/v1",
            kind="Terraform",
            spec=JsonObjectValue({}),
            dependsOn=["Not a name"],
        )


@pytest.mark.parametrize("shape", ["requestedSource", "fromResource", "fromGit", "templateFromPromotion"])
def test_unsupported_stack_template_acquisition_and_source_shapes_are_rejected(shape: str):
    authored = template_document([unit_resource("app", {})], [])
    if shape == "requestedSource":
        document = desired_template_document(authored)
        document["spec"] = {**document["spec"], "requestedSource": {"fromGit": {"path": "."}}}
        contract = CORE_CONTRACTS["stack-template-desired"]
    elif shape in {"fromResource", "fromGit"}:
        source = {"name": "preview"} if shape == "fromResource" else {"path": "."}
        document = {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Stack",
            "metadata": {"name": "web"},
            "spec": {"template": {shape: source}},
        }
        contract = CORE_CONTRACTS["stack-authored"]
    else:
        document = {**authored, "spec": {**authored["spec"], "fromPromotion": {"name": "preview"}}}
        contract = CORE_CONTRACTS["stack-template-authored"]

    with pytest.raises(ContractError):
        contract.parse(document)


def test_stack_only_input_references_an_existing_target_template_by_name():
    authored = template_document([unit_resource("app", {})], [])
    template = CORE_CONTRACTS["stack-template-authored"].parse(authored)
    stack = CORE_CONTRACTS["stack-authored"].parse(
        {
            "apiVersion": "gitopsctr.io/v1",
            "kind": "Stack",
            "metadata": {"name": "web"},
            "spec": {"template": template.metadata.name},
        }
    )

    assert stack.spec.template == template.metadata.name


@pytest.mark.parametrize(
    "spec",
    [
        {
            "source": {
                "fromGit": {
                    "repository": "https://example.invalid/templates.git",
                    "revision": "main",
                    "path": "stacks/web.yaml",
                    "documentDigest": "sha256:" + "a" * 64,
                }
            }
        },
        {"source": {"fromPromotion": {"stack": "application"}}},
    ],
)
def test_repository_and_promotion_stack_template_sources_are_typed(spec):
    document = {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "StackTemplate",
        "metadata": {"name": "preview"},
        "spec": spec,
    }
    parsed = CORE_CONTRACTS["stack-template-authored"].parse(document)
    assert (
        parsed.spec
        == CORE_CONTRACTS["stack-template-authored"].parse(CORE_CONTRACTS["stack-template-authored"].dump(parsed)).spec
    )


@pytest.mark.parametrize(
    "path", ["", "/template.yaml", "templates//web.yaml", "templates/./web.yaml", "../web.yaml", "templates\\web.yaml"]
)
def test_stack_template_repository_paths_are_safe_relative_posix_paths(path):
    document = {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "StackTemplate",
        "metadata": {"name": "preview"},
        "spec": {
            "source": {
                "fromGit": {
                    "repository": "https://example.invalid/templates.git",
                    "revision": "main",
                    "path": path,
                }
            }
        },
    }
    with pytest.raises(ContractError):
        CORE_CONTRACTS["stack-template-authored"].parse(document)


def test_stack_template_acquisition_preserves_requested_and_resolved_lineage_variants():
    authored = template_document([unit_resource("app", {})], [])
    typed = CORE_CONTRACTS["stack-template-authored"].parse(authored)
    desired = DesiredStackTemplateDocument(
        apiVersion="gitopsctr.io/v1",
        kind="StackTemplate",
        metadata=DesiredResourceMetadata(name="preview", uid="template-uid"),
        spec=DesiredStackTemplateSpec(
            parameters=typed.spec.parameters,
            unitTemplates=typed.spec.unitTemplates,
            contentDigest=typed.spec.semantic_content_digest(),
            acquisition=StackTemplateAcquisition(
                documentDigest="sha256:" + "b" * 64,
                requestedSource=StackTemplateRequestedFromGit(
                    fromGit=StackTemplateGitRequest(
                        repository="https://example.invalid/templates.git",
                        revision="main",
                        path="stacks/web.yaml",
                    )
                ),
                resolvedSource=StackTemplateResolvedFromGitSource(
                    fromGit=StackTemplateResolvedFromGit(
                        repository="https://example.invalid/templates.git",
                        revision="a" * 40,
                        path="stacks/web.yaml",
                    )
                ),
            ),
            sourceContext=StackTemplateSourceContext(
                repository="https://example.invalid/templates.git",
                revision="a" * 40,
            ),
        ),
    )
    dumped = CORE_CONTRACTS["stack-template-desired"].dump(desired)
    assert CORE_CONTRACTS["stack-template-desired"].parse(dumped) == desired

    promotion = replace(
        desired.spec,
        acquisition=StackTemplateAcquisition(
            documentDigest="sha256:" + "c" * 64,
            requestedSource=StackTemplateRequestedFromPromotion(
                fromPromotion=StackTemplatePromotionRequest(stack="application")
            ),
            resolvedSource=StackTemplateResolvedFromPromotionSource(
                fromPromotion=StackTemplateResolvedFromPromotion(
                    environment="staging",
                    desiredRef="refs/heads/main",
                    desiredRevision="d" * 40,
                    stack="application",
                    stackUid="stack-uid",
                    template="preview",
                    templateUid="source-template-uid",
                    templateContentDigest=desired.spec.contentDigest,
                )
            ),
        ),
        sourceContext=None,
    )
    assert (
        CORE_CONTRACTS["stack-template-desired"]
        .parse(CORE_CONTRACTS["stack-template-desired"].dump(replace(desired, spec=promotion)))
        .spec.acquisition.requestedSource.fromPromotion.stack
        == "application"
    )


def test_stack_template_acquisition_rejects_mismatched_modes_and_fences_corresponding_fields():
    request = StackTemplateGitRequest(
        repository="https://example.invalid/templates.git", revision="main", path="templates/web.yaml"
    )
    resolved = StackTemplateResolvedFromGit(
        repository="https://other.invalid/templates.git", revision="a" * 40, path="templates/other.yaml"
    )
    with pytest.raises(ValueError, match="repository and path"):
        StackTemplateAcquisition(
            documentDigest="sha256:" + "b" * 64,
            requestedSource=StackTemplateRequestedFromGit(fromGit=request),
            resolvedSource=StackTemplateResolvedFromGitSource(fromGit=resolved),
        )

    with pytest.raises(ValueError, match="modes must match"):
        StackTemplateAcquisition(
            documentDigest="sha256:" + "b" * 64,
            requestedSource=StackTemplateRequestedFromPromotion(
                fromPromotion=StackTemplatePromotionRequest(stack="application")
            ),
            resolvedSource=StackTemplateResolvedFromInput(fromInput=StackTemplateFromInput()),
        )


def test_stack_template_acquisition_rejects_an_exact_requested_git_sha_that_resolves_elsewhere():
    with pytest.raises(ValueError, match="exact requested SHA"):
        StackTemplateAcquisition(
            documentDigest="sha256:" + "b" * 64,
            requestedSource=StackTemplateRequestedFromGit(
                fromGit=StackTemplateGitRequest(
                    repository="https://example.invalid/templates.git",
                    revision="a" * 40,
                    path="templates/web.yaml",
                )
            ),
            resolvedSource=StackTemplateResolvedFromGitSource(
                fromGit=StackTemplateResolvedFromGit(
                    repository="https://example.invalid/templates.git",
                    revision="b" * 40,
                    path="templates/web.yaml",
                )
            ),
        )


def test_desired_git_stack_template_source_context_is_fenced_to_resolution():
    authored = CORE_CONTRACTS["stack-template-authored"].parse(template_document([unit_resource("app", {})], []))
    acquisition = StackTemplateAcquisition(
        documentDigest="sha256:" + "b" * 64,
        requestedSource=StackTemplateRequestedFromGit(
            fromGit=StackTemplateGitRequest(
                repository="https://example.invalid/templates.git", revision="main", path="templates/web.yaml"
            )
        ),
        resolvedSource=StackTemplateResolvedFromGitSource(
            fromGit=StackTemplateResolvedFromGit(
                repository="https://example.invalid/templates.git", revision="a" * 40, path="templates/web.yaml"
            )
        ),
    )
    with pytest.raises(ValueError, match="sourceContext"):
        DesiredStackTemplateSpec(
            parameters=authored.spec.parameters,
            unitTemplates=authored.spec.unitTemplates,
            contentDigest=authored.spec.semantic_content_digest(),
            acquisition=acquisition,
            sourceContext=StackTemplateSourceContext(repository=".", revision="a" * 40),
        )


@pytest.mark.parametrize(
    ("kind", "source", "parameters", "requires_context"),
    [
        ("Terraform", None, [], False),
        ("FrontendS3Cloudfront", None, [], False),
        ("Terraform", {"path": "."}, [], True),
        ("Terraform", {"fromParameter": {"name": "source"}}, [{"name": "source", "type": "object"}], True),
        (
            "Terraform",
            {"path": {"fromParameter": {"name": "source"}}},
            [{"name": "source", "type": "string"}],
            True,
        ),
    ],
)
def test_desired_stack_template_source_context_matrix(
    kind: str, source: object, parameters: list[dict[str, str]], requires_context: bool
):
    authored = CORE_CONTRACTS["stack-template-authored"].parse(
        template_document([unit_resource("app", {"source": source}, kind=kind)], parameters)
    )
    desired_kwargs = {
        "parameters": authored.spec.parameters,
        "unitTemplates": authored.spec.unitTemplates,
        "contentDigest": authored.spec.semantic_content_digest(),
        "acquisition": StackTemplateAcquisition(
            documentDigest="sha256:" + "c" * 64,
            requestedSource=StackTemplateRequestedFromInput(fromInput=StackTemplateFromInput()),
            resolvedSource=StackTemplateResolvedFromInput(fromInput=StackTemplateFromInput()),
        ),
    }
    if requires_context:
        with pytest.raises(ValueError, match="repository-backed Unit sources"):
            DesiredStackTemplateSpec(**desired_kwargs)
        desired_kwargs["sourceContext"] = StackTemplateSourceContext(repository=".", revision="a" * 40)
    desired = DesiredStackTemplateSpec(**desired_kwargs)
    if requires_context:
        assert desired.sourceContext is not None
    else:
        assert desired.sourceContext is None


def test_desired_promotion_template_digest_and_name_are_fenced():
    authored = CORE_CONTRACTS["stack-template-authored"].parse(template_document([unit_resource("app", {})], []))
    digest = authored.spec.semantic_content_digest()

    with pytest.raises(ValueError, match="templateContentDigest"):
        DesiredStackTemplateSpec(
            parameters=authored.spec.parameters,
            unitTemplates=authored.spec.unitTemplates,
            contentDigest=digest,
            acquisition=StackTemplateAcquisition(
                documentDigest="sha256:" + "c" * 64,
                requestedSource=StackTemplateRequestedFromPromotion(
                    fromPromotion=StackTemplatePromotionRequest(stack="application")
                ),
                resolvedSource=StackTemplateResolvedFromPromotionSource(
                    fromPromotion=StackTemplateResolvedFromPromotion(
                        environment="staging",
                        desiredRef="refs/heads/main",
                        desiredRevision="d" * 40,
                        stack="application",
                        stackUid="stack-uid",
                        template="other",
                        templateUid="source-template-uid",
                        templateContentDigest="sha256:" + "e" * 64,
                    )
                ),
            ),
        )

    spec = DesiredStackTemplateSpec(
        parameters=authored.spec.parameters,
        unitTemplates=authored.spec.unitTemplates,
        contentDigest=digest,
        acquisition=StackTemplateAcquisition(
            documentDigest="sha256:" + "c" * 64,
            requestedSource=StackTemplateRequestedFromPromotion(
                fromPromotion=StackTemplatePromotionRequest(stack="application")
            ),
            resolvedSource=StackTemplateResolvedFromPromotionSource(
                fromPromotion=StackTemplateResolvedFromPromotion(
                    environment="staging",
                    desiredRef="refs/heads/main",
                    desiredRevision="d" * 40,
                    stack="application",
                    stackUid="stack-uid",
                    template="other",
                    templateUid="source-template-uid",
                    templateContentDigest=digest,
                )
            ),
        ),
    )
    with pytest.raises(ValueError, match="template name"):
        DesiredStackTemplateDocument(
            apiVersion="gitopsctr.io/v1",
            kind="StackTemplate",
            metadata=DesiredResourceMetadata(name="preview", uid="template-uid"),
            spec=spec,
        )


def test_template_semantic_identity_and_source_context_are_fenced():
    authored = CORE_CONTRACTS["stack-template-authored"].parse(
        template_document(
            [unit_resource("app", {"source": {"path": "."}})],
            [{"name": "region", "type": "string"}],
        )
    )
    digest = authored.spec.semantic_content_digest()
    desired = DesiredStackTemplateSpec(
        parameters=authored.spec.parameters,
        unitTemplates=authored.spec.unitTemplates,
        contentDigest=digest,
        acquisition=StackTemplateAcquisition(
            documentDigest="sha256:" + "c" * 64,
            requestedSource=StackTemplateRequestedFromInput(fromInput=StackTemplateFromInput()),
            resolvedSource=StackTemplateResolvedFromInput(fromInput=StackTemplateFromInput()),
        ),
        sourceContext=StackTemplateSourceContext(
            repository="https://example.invalid/templates.git",
            revision="a" * 40,
        ),
    )
    assert desired.semantic_content_digest() == digest
    assert (
        CORE_CONTRACTS["stack-template-desired"]
        .parse(
            CORE_CONTRACTS["stack-template-desired"].dump(
                DesiredStackTemplateDocument(
                    apiVersion="gitopsctr.io/v1",
                    kind="StackTemplate",
                    metadata=DesiredResourceMetadata(name="preview", uid="template-uid"),
                    spec=desired,
                )
            )
        )
        .spec.sourceContext
        == desired.sourceContext
    )

    with pytest.raises(ValueError, match="does not match"):
        replace(desired, contentDigest="sha256:" + "d" * 64)


def projection_unit(depends_on: list[str] | None = None) -> StackProjectionUnit:
    return StackProjectionUnit(
        apiVersion="unit.gitopsctr.io/v1",
        kind="Terraform",
        spec=JsonObjectValue({}),
        dependsOn=[] if depends_on is None else depends_on,
    )


def test_structural_projection_is_required_identity_fenced_and_round_trips():
    projection = StackProjection.build(
        stack_uid="stack-uid",
        template_uid="template-uid",
        template_content_digest="sha256:" + "a" * 64,
        context_digest=TEST_PROJECTION_CONTEXT_DIGEST,
        units={"app": projection_unit()},
    )
    assert projection.identity.projectionDigest.startswith("sha256:")
    assert StackProjection.from_dict(projection.to_dict()) == projection

    with pytest.raises(ValueError, match="projectionDigest"):
        StackProjection(
            identity=replace(projection.identity, projectionDigest="sha256:" + "b" * 64),
            units=projection.units,
        )


def test_structural_projection_build_requires_context_digest():
    with pytest.raises(TypeError, match="context_digest"):
        StackProjection.build(
            stack_uid="stack-uid",
            template_uid="template-uid",
            template_content_digest="sha256:" + "a" * 64,
            units={},
        )


def test_structural_projection_schema_and_runtime_reject_from_parameter():
    projection = StackProjection.build(
        stack_uid="stack-uid",
        template_uid="template-uid",
        template_content_digest="sha256:" + "a" * 64,
        context_digest=TEST_PROJECTION_CONTEXT_DIGEST,
        units={"app": projection_unit()},
    )
    document = CORE_CONTRACTS["stack-desired"].dump(
        DesiredStackDocument(
            apiVersion="gitopsctr.io/v1",
            kind="Stack",
            metadata=DesiredResourceMetadata(name="web", uid="stack-uid"),
            spec=DesiredStackSpec(
                templateRef=StackTemplateReference(
                    name="preview",
                    uid="template-uid",
                    contentDigest="sha256:" + "a" * 64,
                ),
                structuralProjection=projection,
            ),
        )
    )
    document["spec"]["structuralProjection"]["units"]["app"]["spec"] = {"nested": {"fromParameter": {"name": "value"}}}
    with pytest.raises(ContractError):
        CORE_CONTRACTS["stack-desired"].parse(document)

    document["spec"]["structuralProjection"]["units"]["app"]["spec"] = {
        "nested": {
            "fromReceipt": {
                "unit": "producer",
                "dryFallback": {"fromParameter": {"name": "value"}},
            }
        }
    }
    with pytest.raises(ContractError):
        CORE_CONTRACTS["stack-desired"].parse(document)


@pytest.mark.parametrize(
    "units",
    [
        {"app": projection_unit(["missing"])},
        {"app": projection_unit(["db"]), "db": projection_unit(["app"])},
        {"app": projection_unit(["app"])},
    ],
)
def test_structural_projection_rejects_missing_self_and_cyclic_dependencies(units):
    with pytest.raises(ValueError, match="dependencies|depend on itself"):
        StackProjection.build(
            stack_uid="stack-uid",
            template_uid="template-uid",
            template_content_digest="sha256:" + "a" * 64,
            context_digest=TEST_PROJECTION_CONTEXT_DIGEST,
            units=units,
        )


def _active_binding(name: str, depends_on: list[str] | None = None) -> StackProjectionUnitBinding:
    return StackProjectionUnitBinding(
        apiVersion="unit.gitopsctr.io/v1",
        kind="Terraform",
        name=name,
        uid=f"uid-{name}",
        desiredDigest="sha256:" + "b" * 64,
        sourceProjectionDigest="sha256:" + "c" * 64,
        projectionContextDigest=TEST_PROJECTION_CONTEXT_DIGEST,
        dependsOn=[] if depends_on is None else depends_on,
    )


@pytest.mark.parametrize(
    "units",
    [
        {"a": _active_binding("a"), "b": _active_binding("a")},
        {"a": _active_binding("a", ["missing"]), "b": _active_binding("b")},
        {"a": _active_binding("a", ["a"]), "b": _active_binding("b")},
        {"a": _active_binding("a", ["b"]), "b": _active_binding("b", ["a"])},
    ],
)
def test_active_projection_direct_construction_rejects_invalid_concrete_graph(units):
    with pytest.raises(ValueError, match="duplicate|unknown|itself|acyclic"):
        StackActiveProjection.build(
            source_projection_digest="sha256:" + "a" * 64,
            projection_context_digest="sha256:" + "c" * 64,
            units=units,
        )


def test_desired_stack_requires_exact_active_dependencies_for_current_structure():
    structural = StackProjection.build(
        stack_uid="stack-uid",
        template_uid="template-uid",
        template_content_digest="sha256:" + "a" * 64,
        context_digest=TEST_PROJECTION_CONTEXT_DIGEST,
        units={"db": projection_unit(), "app": projection_unit(["db"])},
    )
    active = StackActiveProjection.build(
        source_projection_digest=structural.identity.projectionDigest,
        projection_context_digest=structural.identity.projectionContextDigest,
        units={
            "db": replace(
                _active_binding("db"),
                sourceProjectionDigest=structural.identity.projectionDigest,
                projectionContextDigest=structural.identity.projectionContextDigest,
            ),
            "app": replace(
                _active_binding("app"),
                sourceProjectionDigest=structural.identity.projectionDigest,
                projectionContextDigest=structural.identity.projectionContextDigest,
            ),
        },
    )
    with pytest.raises(ValueError, match="dependencies do not match"):
        DesiredStackSpec(
            templateRef=StackTemplateReference(
                name="preview",
                uid="template-uid",
                contentDigest="sha256:" + "a" * 64,
            ),
            structuralProjection=structural,
            activeProjection=active,
        )


@pytest.mark.parametrize(
    ("parameter_type", "value"),
    [
        ("string", "web"),
        ("integer", 3),
        ("number", 3.5),
        ("boolean", True),
        ("object", {"nested": [1, False]}),
        ("array", ["web", 3]),
    ],
)
def test_parameter_types_validate_and_expand_recursively(parameter_type: str, value: object):
    raw = template_document(
        [
            unit_resource(
                "app",
                {
                    "values": [
                        {"fromParameter": {"name": "value"}},
                        {"nested": {"fromParameter": {"name": "value"}}},
                    ]
                },
            )
        ],
        [{"name": "value", "type": parameter_type}],
    )
    template = CORE_CONTRACTS["stack-template-authored"].parse(raw)
    expanded = template.spec.expand({"value": value})
    assert expanded[0].spec["values"] == [value, {"nested": value}]


def test_parameter_expansion_recurses_into_dynamic_reference_dry_fallback():
    raw = template_document(
        [
            unit_resource(
                "app",
                {
                    "value": {
                        "fromPromotion": {
                            "unit": "source",
                            "dryFallback": {"nested": {"fromParameter": {"name": "fallback"}}},
                        }
                    }
                },
            )
        ],
        [{"name": "fallback", "type": "string"}],
    )
    template = CORE_CONTRACTS["stack-template-authored"].parse(raw)
    expanded = template.spec.expand({"fallback": "bootstrap"})
    reference = expanded[0].spec["value"]
    assert isinstance(reference, PromotionReference)
    assert dump_template_value(reference)["fromPromotion"]["dryFallback"] == {"nested": "bootstrap"}


@pytest.mark.parametrize(
    "reference",
    [
        {"fromReceipt": {"unit": "producer", "dryFallback": {"nested": [{"fromParameter": {"name": "x"}}]}}},
        {
            "fromArtifact": {
                "unit": "producer",
                "name": "artifact",
                "apiVersion": "artifact.gitopsctr.io/v1",
                "kind": "ContainerImages",
                "dryFallback": {"nested": [{"fromParameter": {"name": "x"}}]},
            }
        },
        {"fromPromotion": {"dryFallback": {"nested": [{"fromParameter": {"name": "x"}}]}}},
    ],
)
def test_projection_runtime_rejects_parameters_inside_reference_fallbacks(reference):
    with pytest.raises(TemplateError, match="fromParameter"):
        parse_projection_value({"value": reference})
    with pytest.raises(ValueError, match="fromParameter"):
        ProjectionObject._deserialize({"value": reference})


@pytest.mark.parametrize(
    "values",
    [{}, {"value": "ok", "other": True}, {"value": True}],
)
def test_parameter_validation_rejects_missing_unknown_and_invalid_values(values):
    template = CORE_CONTRACTS["stack-template-authored"].parse(
        template_document([unit_resource("app", {})], [{"name": "value", "type": "string"}])
    )
    with pytest.raises(TemplateError):
        template.spec.expand(values)


def test_resource_graph_rejects_missing_and_cyclic_template_dependencies():
    with pytest.raises(ContractError):
        CORE_CONTRACTS["stack-template-authored"].parse(
            template_document([unit_resource("app", {}, dependsOn=["missing"])], [])
        )
    with pytest.raises(ContractError):
        CORE_CONTRACTS["stack-template-authored"].parse(
            template_document([unit_resource("a", {}, dependsOn=["b"]), unit_resource("b", {}, dependsOn=["a"])], [])
        )


def test_parameter_expression_schema_is_recursive_and_published_in_catalog():
    schema = schemas.core_resource_schema("StackTemplate", "authored")
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(
        template_document(
            [unit_resource("app", {"nested": [{"fromParameter": {"name": "value"}}]})],
            [{"name": "value", "type": "array"}],
        )
    )
    documents = schemas.schema_documents()
    assert documents[Path("apis/gitopsctr.io/v1/StackTemplate/authored.schema.json")]["$id"].endswith(
        "/StackTemplate/authored.schema.json"
    )


def test_environment_reference_schema_matches_runtime_contract():
    schema = schemas.core_resource_schema("StackTemplate", "authored")
    environment = schema["$defs"]["TemplateValue"]["oneOf"]
    environment_variant = next(
        item for item in environment if item.get("properties", {}).get("fromEnvironment") is not None
    )
    target = environment_variant["properties"]["fromEnvironment"]
    assert "dryFallback" not in target["properties"]
    with pytest.raises(ContractError):
        CORE_CONTRACTS["stack-template-authored"].parse(
            template_document(
                [unit_resource("app", {"value": {"fromEnvironment": {"dryFallback": "x"}}})],
                [],
            )
        )


def test_promoted_artifact_lineage_requires_a_git_receipt_blob():
    contract = MashumaroContract(ResolvedArtifactImport, "urn:test:resolved-artifact-import")
    with pytest.raises(ContractError, match="does not match"):
        contract.parse(
            {
                "sourceStack": "application",
                "sourceStackUid": "uid-stack",
                "sourceUnit": "images",
                "sourceUnitUid": "uid-images",
                "sourceDesiredRevision": "a" * 40,
                "sourceObservedRevision": "b" * 40,
                "receiptUnitBlob": "c" * 64,
                "artifactName": "containers",
                "apiVersion": "artifact.gitopsctr.io/v1",
                "kind": "ContainerImages",
                "artifactDigest": "sha256:" + "d" * 64,
                "targetStackUid": "uid-target",
                "artifactDocument": JsonObjectValue({}),
            }
        )
