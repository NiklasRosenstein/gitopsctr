"""Shared desired-state fixtures for Stack deletion tests."""

from __future__ import annotations

import hashlib
import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from gitopsctr import controller
from gitopsctr.contracts import (
    DesiredStackSpec,
    DesiredStackTemplateSpec,
    StackActiveProjection,
    StackProjection,
    StackProjectionUnit,
    StackProjectionUnitBinding,
    StackTemplateAcquisition,
    StackTemplateFromInput,
    StackTemplateReference,
    StackTemplateRequestedFromInput,
    StackTemplateResolvedFromInput,
    StackTemplateSpec,
    StackTemplateUnitTemplate,
)
from gitopsctr.document import JsonObjectValue
from gitopsctr.resources import ResourceMetadata, StackResource
from gitopsctr.templates import TemplateObject


def stack_tree(root: Path) -> tuple[str, str]:
    """Create a partitioned Stack with one UID-owned generated Unit."""
    stack_uid = "d1-stack-preview"
    root.mkdir(parents=True)
    (root / "gitopsctr.yaml").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Project",
                "metadata": {"name": "test"},
                "spec": {"effectLease": None, "writeFormat": "json"},
            }
        )
    )
    environment = root / "deployment/environments/dev"
    environment.mkdir(parents=True)
    (environment / "environment.json").write_text(
        json.dumps(
            {
                "apiVersion": "gitopsctr.io/v1",
                "kind": "Environment",
                "metadata": {"name": "dev"},
                "spec": {},
            }
        )
    )
    context = controller.capture_projection_context(root, "dev")
    controller.write_projection_context(root, context)
    template_content = StackTemplateSpec(
        parameters=[],
        unitTemplates={
            "preview-app": StackTemplateUnitTemplate(
                apiVersion=controller.UNIT_API_VERSION,
                kind="Terraform",
                spec=TemplateObject({}),
            ),
        },
    )
    template_spec = DesiredStackTemplateSpec(
        parameters=[],
        unitTemplates=template_content.unitTemplates,
        contentDigest=template_content.semantic_content_digest(),
        acquisition=StackTemplateAcquisition(
            documentDigest="sha256:" + "b" * 64,
            requestedSource=StackTemplateRequestedFromInput(fromInput=StackTemplateFromInput()),
            resolvedSource=StackTemplateResolvedFromInput(fromInput=StackTemplateFromInput()),
        ),
    )
    template = StackResource(
        controller.GVK(controller.CORE_API_VERSION, "StackTemplate"),
        ResourceMetadata(name="preview", uid="d1-template").with_partition("preview"),
        template_spec,
    )
    projection = StackProjection.build(
        stack_uid=stack_uid,
        template_uid="d1-template",
        template_content_digest=template_spec.contentDigest,
        context_digest=cast(str, context["digest"]),
        units={
            "preview-app": StackProjectionUnit(
                apiVersion=controller.UNIT_API_VERSION,
                kind="Terraform",
                spec=JsonObjectValue({}),
                dependsOn=[],
            )
        },
    )
    unit = controller.RESOURCE_CATALOG.parse_unit(
        {
            "apiVersion": controller.UNIT_API_VERSION,
            "kind": "Terraform",
            "metadata": {
                "name": "preview-app",
                "uid": "d1-preview-app",
                "ownerReferences": [
                    {
                        "apiVersion": controller.CORE_API_VERSION,
                        "kind": "Stack",
                        "name": "preview",
                        "uid": stack_uid,
                    }
                ],
            },
            "spec": {
                "source": {
                    "path": ".",
                    "revision": "a" * 40,
                    "inputHash": "sha256:" + "0" * 64,
                    "driverVersion": controller.DRIVER_VERSIONS["terraform"],
                },
                "terraform": {"backend": {}, "variables": {}, "observeOutputs": []},
            },
        },
        profile="desired",
        expected_name="preview-app",
    )
    active_projection = StackActiveProjection.build(
        source_projection_digest=projection.identity.projectionDigest,
        projection_context_digest=projection.identity.projectionContextDigest,
        units={
            "preview-app": StackProjectionUnitBinding(
                apiVersion=unit.gvk.api_version,
                kind=unit.gvk.kind,
                name=unit.name,
                uid=unit.metadata.uid or "",
                desiredDigest=controller.desired_unit_binding_digest(unit),
                sourceProjectionDigest=projection.identity.projectionDigest,
                projectionContextDigest=projection.identity.projectionContextDigest,
            )
        },
    )
    stack = StackResource(
        controller.GVK(controller.CORE_API_VERSION, "Stack"),
        ResourceMetadata(name="preview", uid=stack_uid).with_partition("preview"),
        DesiredStackSpec(
            templateRef=StackTemplateReference(
                name="preview", uid="d1-template", contentDigest=template_spec.contentDigest
            ),
            parameters=JsonObjectValue({}),
            structuralProjection=projection,
            activeProjection=active_projection,
        ),
    )
    (root / "stack-templates").mkdir()
    (root / "stacks").mkdir()
    (root / "stack-templates/preview.json").write_text(
        json.dumps(controller.RESOURCE_CATALOG.serialize_stack_resource(template, profile="desired"))
    )
    (root / "stacks/preview.json").write_text(
        json.dumps(controller.RESOURCE_CATALOG.serialize_stack_resource(stack, profile="desired"))
    )
    controller.write_desired_candidate_unit(root / "units/preview/preview-app.json", unit, root)
    return stack_uid, "preview/preview-app"


def deletion_args(**overrides: object) -> Namespace:
    """Build standard Stack deletion command arguments."""
    values = {
        "environment": "dev",
        "stack": "preview",
        "name": "preview",
        "kind": "Stack",
        "uid": "d1-stack-preview",
        "desired_ref": "deploy/dev",
        "observed_ref": None,
        "candidate_ref": None,
        "dry": False,
        "deletion_generation": 1,
    }
    values.update(overrides)
    return Namespace(**values)


def fake_git(*args: str, **_kwargs: object) -> SimpleNamespace:
    """Minimal Git double used by publication tests."""
    if args[0] == "hash-object":
        return SimpleNamespace(stdout=hashlib.sha1(Path(args[1]).read_bytes()).hexdigest() + "\n", returncode=0)
    return SimpleNamespace(stdout="", returncode=0)
