from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from gitopsctr import schemas
from gitopsctr.contracts import (
    CORE_CONTRACTS,
    AuthoredResourceMetadata,
    DesiredLifecycle,
    DesiredResourceMetadata,
    DesiredStackDocument,
    DesiredStackSpec,
    LifecycleManagement,
    StackDocument,
    StackInstantiationProvenance,
    StackSpec,
    StackTemplateDocument,
    StackTemplateResource,
    StackTemplateSpec,
)
from gitopsctr.document import ContractError, JsonObjectValue
from gitopsctr.templates import ParameterTemplateObject, TemplateError


def template_document(resources: list[dict[str, object]], parameters: list[dict[str, str]]) -> dict[str, object]:
    return {
        "apiVersion": "gitopsctr.io/v1",
        "kind": "StackTemplate",
        "metadata": {"name": "preview"},
        "spec": {"parameters": parameters, "resources": resources},
    }


def unit_resource(name: str, spec: dict[str, object], **extra: object) -> dict[str, object]:
    return {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "Terraform",
        "name": name,
        "spec": spec,
        **extra,
    }


def test_stack_and_stack_template_authored_and_desired_contracts_are_typed():
    authored = template_document(
        [unit_resource("app", {"source": {"path": "."}})],
        [{"name": "name", "type": "string"}],
    )
    parsed = CORE_CONTRACTS["stack-template-authored"].parse(authored)
    assert isinstance(parsed, StackTemplateDocument)
    assert parsed.metadata == AuthoredResourceMetadata(name="preview")

    desired = {
        **authored,
        "metadata": {
            "name": "preview",
            "uid": "d1-preview",
            "lifecycle": {"management": {"mode": "sourceTracked"}},
        },
    }
    desired_parsed = CORE_CONTRACTS["stack-template-desired"].parse(desired)
    assert isinstance(desired_parsed.metadata, DesiredResourceMetadata)
    assert desired_parsed.metadata.lifecycle == DesiredLifecycle(management=LifecycleManagement(mode="sourceTracked"))

    stack = StackDocument(
        apiVersion="gitopsctr.io/v1",
        kind="Stack",
        metadata=AuthoredResourceMetadata(name="preview"),
        spec=StackSpec(template="preview", parameters=JsonObjectValue({"name": "web"})),
    )
    assert CORE_CONTRACTS["stack-authored"].parse(CORE_CONTRACTS["stack-authored"].dump(stack)) == stack


def test_direct_stack_provenance_is_desired_only_and_round_trips():
    provenance = StackInstantiationProvenance(
        templateRevision="a" * 40,
        templatePath="deployment/environments/dev/stack-templates/preview.yaml",
        templateDigest="b" * 64,
        requestIdentity="pull-123",
    )
    desired = DesiredStackDocument(
        apiVersion="gitopsctr.io/v1",
        kind="Stack",
        metadata=DesiredResourceMetadata(
            name="preview",
            uid="d1-preview",
            lifecycle=DesiredLifecycle(management=LifecycleManagement(mode="direct")),
        ),
        spec=DesiredStackSpec(
            template="preview",
            parameters=JsonObjectValue({"namespace": "preview-123"}),
            provenance=provenance,
        ),
    )
    assert CORE_CONTRACTS["stack-desired"].parse(CORE_CONTRACTS["stack-desired"].dump(desired)) == desired
    with pytest.raises(ContractError):
        CORE_CONTRACTS["stack-authored"].parse(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Stack",
                "metadata": {"name": "preview"},
                "spec": {
                    "template": "preview",
                    "parameters": {},
                    "provenance": {
                        "templateRevision": "a" * 40,
                        "templatePath": "preview.yaml",
                        "templateDigest": "b" * 64,
                        "requestIdentity": "pull-123",
                    },
                },
            }
        )


def test_direct_stack_provenance_accepts_canonical_github_request_identity():
    provenance = StackInstantiationProvenance(
        templateRevision="a" * 40,
        templatePath="deployment/environments/dev/stack-templates/preview.yaml",
        templateDigest="b" * 64,
        requestIdentity="github:example-org/application#123",
    )
    assert provenance.requestIdentity == "github:example-org/application#123"


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
    assert expanded[0].name == "app"
    assert expanded[0].spec["values"] == [value, {"nested": value}]


@pytest.mark.parametrize(
    "values, message",
    [
        ({}, "missing"),
        ({"value": "ok", "other": True}, "unknown"),
        ({"value": True}, "must be string"),
    ],
)
def test_parameter_validation_rejects_missing_unknown_and_invalid_values(values, message):
    template = CORE_CONTRACTS["stack-template-authored"].parse(
        template_document([unit_resource("app", {})], [{"name": "value", "type": "string"}])
    )
    with pytest.raises(TemplateError, match=message):
        template.spec.expand(values)


def test_expansion_preserves_order_does_not_mutate_authored_input_and_rejects_non_units():
    raw = template_document(
        [
            unit_resource("second", {"value": {"fromParameter": {"name": "value"}}}, dependsOn=["first"]),
            unit_resource("first", {"value": "fixed"}),
        ],
        [{"name": "value", "type": "object"}],
    )
    original = deepcopy(raw)
    template = CORE_CONTRACTS["stack-template-authored"].parse(raw)
    expanded = template.spec.expand({"value": {"nested": [1]}})
    assert [resource.name for resource in expanded] == ["second", "first"]
    assert raw == original
    assert expanded[0].spec["value"] == {"nested": [1]}

    non_unit = StackTemplateSpec(
        parameters=[],
        resources=[
            StackTemplateResource(
                apiVersion="gitopsctr.io/v1",
                kind="Stack",
                name="nested",
                spec=ParameterTemplateObject({}),
            )
        ],
    )
    with pytest.raises(TemplateError, match="installed Unit"):
        non_unit.expand({})


@pytest.mark.parametrize(
    "resources, message",
    [
        ([unit_resource("app", {}), unit_resource("app", {})], "unique"),
        ([unit_resource("app", {}, dependsOn=["missing"])], "missing dependencies"),
        (
            [unit_resource("a", {}, dependsOn=["b"]), unit_resource("b", {}, dependsOn=["a"])],
            "acyclic",
        ),
    ],
)
def test_resource_graph_rejects_duplicate_missing_and_cyclic_dependencies(resources, message):
    with pytest.raises((ContractError, ValueError)):
        CORE_CONTRACTS["stack-template-authored"].parse(template_document(resources, []))


def test_parameter_expression_schema_is_recursive_and_published_in_catalog():
    schema = schemas.core_resource_schema("StackTemplate", "authored")
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(
        template_document(
            [unit_resource("app", {"nested": [{"fromParameter": {"name": "value"}}]})],
            [{"name": "value", "type": "array"}],
        )
    )
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(
            template_document([unit_resource("app", {"bad": {"fromReceipt": {"unit": "db"}}})], [])
        )

    documents = schemas.schema_documents()
    assert documents[Path("apis/gitopsctr.io/v1/StackTemplate/authored.schema.json")]["$id"].endswith(
        "/StackTemplate/authored.schema.json"
    )
