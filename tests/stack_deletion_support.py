"""Shared desired-state fixtures for direct Stack deletion tests."""

from __future__ import annotations

import hashlib
import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

from gitopsctr import controller
from gitopsctr.contracts import (
    DesiredLifecycle,
    LifecycleManagement,
    StackInstantiationProvenance,
    StackTemplateResource,
    StackTemplateSpec,
)
from gitopsctr.document import JsonObjectValue
from gitopsctr.resources import ResourceMetadata, StackResource
from gitopsctr.templates import ParameterTemplateObject


def stack_tree(root: Path) -> tuple[str, str]:
    """Create a direct Stack with one UID-owned generated Unit."""
    stack_uid = "d1-stack-direct"
    template = StackResource(
        controller.GVK(controller.CORE_API_VERSION, "StackTemplate"),
        ResourceMetadata(
            name="preview",
            uid="d1-template",
            lifecycle=DesiredLifecycle(management=LifecycleManagement(mode="sourceTracked")),
        ),
        StackTemplateSpec(
            parameters=[],
            resources=[
                StackTemplateResource(
                    apiVersion=controller.UNIT_API_VERSION,
                    kind="Terraform",
                    name="preview-app",
                    spec=ParameterTemplateObject({}),
                ),
            ],
        ),
    )
    provenance = StackInstantiationProvenance(
        templateRevision="a" * 40,
        templatePath="deployment/environments/dev/stack-templates/preview.yaml",
        templateDigest="b" * 64,
        requestIdentity="pull-123",
    )
    stack = StackResource(
        controller.GVK(controller.CORE_API_VERSION, "Stack"),
        ResourceMetadata(
            name="preview",
            uid=stack_uid,
            lifecycle=DesiredLifecycle(management=LifecycleManagement(mode="direct")),
        ),
        controller.DesiredStackSpec(template="preview", parameters=JsonObjectValue({}), provenance=provenance),
    )
    root.mkdir(parents=True)
    (root / "stack-templates").mkdir()
    (root / "stacks").mkdir()
    (root / "stack-templates/preview.json").write_text(
        json.dumps(controller.RESOURCE_CATALOG.serialize_stack_resource(template, profile="desired"))
    )
    (root / "stacks/preview.json").write_text(
        json.dumps(controller.RESOURCE_CATALOG.serialize_stack_resource(stack, profile="desired"))
    )
    unit = controller.RESOURCE_CATALOG.parse_unit(
        {
            "apiVersion": controller.UNIT_API_VERSION,
            "kind": "Terraform",
            "metadata": {
                "name": "preview--preview-app",
                "uid": "d1-preview-app",
                "lifecycle": {
                    "owner": {
                        "apiVersion": controller.CORE_API_VERSION,
                        "kind": "Stack",
                        "name": "preview",
                        "uid": stack_uid,
                    }
                },
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
        expected_name="preview--preview-app",
    )
    controller.write_desired_candidate_unit(root / "units/preview--preview-app.json", unit, root)
    return stack_uid, "preview--preview-app"


def deletion_args(**overrides: object) -> Namespace:
    """Build standard direct Stack deletion command arguments."""
    values = {
        "environment": "dev",
        "stack": "preview",
        "uid": "d1-stack-direct",
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
