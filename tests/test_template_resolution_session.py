"""Authenticated candidate/observation tests for template resolution sessions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any, cast

import pytest

from gitopsctr.application.apply_compilers import (
    PendingTemplateReference,
    ProjectionCompilerError,
    TemplateResolutionSession,
)
from gitopsctr.application.apply_projection import (
    ApplyProjectionContext,
    ApplyProjectionPolicy,
    ProjectedDocument,
)
from gitopsctr.application.model import ChannelId, EnvironmentId
from gitopsctr.application.workspace import InMemoryWorkspace, WorkspaceEntry, entry_content_id
from gitopsctr.contracts import DesiredOwnerReference, DesiredSource
from gitopsctr.contrib.drivers.oci_images import OciImagesDesiredUnit
from gitopsctr.contrib.drivers.terraform import TerraformDesiredUnit
from gitopsctr.errors import ReferenceUnavailable
from gitopsctr.registry import DRIVER_GVKS, DRIVER_NAMES_BY_GVK, UNIT_DRIVERS
from gitopsctr.resolution import TemplateResolution
from gitopsctr.resource_api import GVK, JsonObject
from gitopsctr.resources import CORE_API_VERSION, ResourceCatalog, ResourceMetadata, UnitResource

CATALOG = ResourceCatalog(UNIT_DRIVERS, DRIVER_NAMES_BY_GVK, DRIVER_GVKS)
REVISION = "a" * 40
INPUT_HASH = "sha256:" + "b" * 64
QUALIFIED = "application/producer"


def _context() -> ApplyProjectionContext:
    return ApplyProjectionContext(
        EnvironmentId("dev"),
        ChannelId("desired/dev"),
        ChannelId("observed/dev"),
        ChannelId("candidate/dev"),
        ApplyProjectionPolicy(),
    )


def _desired_unit(driver_name: str = "terraform") -> UnitResource[Any]:
    driver = UNIT_DRIVERS[driver_name]
    source = DesiredSource(path=".", revision=REVISION, inputHash=INPUT_HASH, driverVersion=driver.version)
    specification = (
        TerraformDesiredUnit(source=source) if driver_name == "terraform" else OciImagesDesiredUnit(source=source)
    )
    return UnitResource(
        GVK(driver.api_version, driver.kind),
        ResourceMetadata(
            name="producer",
            uid="d1-application-producer",
            ownerReferences=[
                DesiredOwnerReference(
                    apiVersion=CORE_API_VERSION,
                    kind="Stack",
                    name="application",
                    uid="d1-application",
                )
            ],
        ),
        driver,
        specification,
    )


def _projected(unit: UnitResource[Any]) -> ProjectedDocument:
    return ProjectedDocument("units/application/producer.json", CATALOG.serialize_unit(unit, profile="desired"))


def _content_id(projected: ProjectedDocument) -> str:
    raw = json.dumps(projected.mutable_document(), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return entry_content_id(WorkspaceEntry.file(projected.key, raw)).value


def _receipt(
    unit: UnitResource[Any],
    projected: ProjectedDocument,
    *,
    unit_content_id: str | None = None,
    qualified_name: str = QUALIFIED,
    artifacts: dict[str, JsonObject] | None = None,
) -> JsonObject:
    result: JsonObject = (
        {"applied": {"sourceRevision": REVISION}, "outputs": {"value": "ready"}}
        if unit.driver_name == "terraform"
        else {}
    )
    status: JsonObject = {"controller": {}, "result": result}
    if artifacts is not None:
        status["artifacts"] = cast(Any, artifacts)
    return {
        "apiVersion": CORE_API_VERSION,
        "kind": "Receipt",
        "metadata": {"name": unit.name},
        "spec": {
            "subject": {
                "apiVersion": unit.gvk.api_version,
                "kind": unit.gvk.kind,
                "name": unit.name,
                "qualifiedName": qualified_name,
            },
            "desired": {"unitContentId": unit_content_id or _content_id(projected)},
        },
        "status": status,
    }


def _entry(key: str, document: JsonObject) -> WorkspaceEntry:
    return WorkspaceEntry.file(key, json.dumps(document, sort_keys=True, separators=(",", ":")).encode())


def _session(
    observed: InMemoryWorkspace,
    unit: UnitResource[Any],
    projected: ProjectedDocument,
    *,
    record: bool = True,
) -> TemplateResolutionSession:
    session = TemplateResolutionSession.begin(CATALOG, observed)
    session.declare(QUALIFIED)
    if record:
        session.record(unit, projected)
    return session


def _resolve_receipt(session: TemplateResolutionSession, consumer: UnitResource[Any]) -> TemplateResolution:
    return session.resolve(
        {"fromReceipt": {"unit": QUALIFIED, "pointer": "/outputs/value"}},
        "/inputs/value",
        unit=consumer,
        context=_context(),
    )


def test_receipt_resolution_binds_exact_projected_unit_content_and_typed_subject() -> None:
    producer = _desired_unit()
    projected = _projected(producer)
    observed = InMemoryWorkspace(
        (_entry("units/application/producer.json", _receipt(producer, projected)),), mutable=False
    )
    resolved = _resolve_receipt(_session(observed, producer, projected), _desired_unit())
    assert resolved.value == "ready"
    assert resolved.receipts[QUALIFIED].startswith("sha256:")

    stale = replace(
        producer,
        spec=TerraformDesiredUnit(source=replace(producer.spec.source, inputHash="sha256:" + "c" * 64)),
    )
    with pytest.raises(PendingTemplateReference, match="stale"):
        _resolve_receipt(_session(observed, stale, _projected(stale)), _desired_unit())


def test_receipt_resolution_retries_pending_producer_but_rejects_unknown_and_tampering() -> None:
    producer = _desired_unit()
    projected = _projected(producer)
    receipt = _receipt(producer, projected)
    observed = InMemoryWorkspace((_entry("units/application/producer.json", receipt),), mutable=False)
    session = _session(observed, producer, projected, record=False)
    with pytest.raises(PendingTemplateReference, match="pending projection"):
        _resolve_receipt(session, _desired_unit())
    session.record(producer, projected)
    assert _resolve_receipt(session, _desired_unit()).value == "ready"

    unknown = TemplateResolutionSession.begin(CATALOG, observed)
    with pytest.raises(ReferenceUnavailable, match="not selected"):
        _resolve_receipt(unknown, _desired_unit())

    tampered = _receipt(producer, projected, qualified_name="other/producer")
    with pytest.raises(ReferenceUnavailable, match="foreign typed Unit identity"):
        _resolve_receipt(
            _session(
                InMemoryWorkspace((_entry("units/application/producer.json", tampered),), mutable=False),
                producer,
                projected,
            ),
            _desired_unit(),
        )
    wrong_document = ProjectedDocument(
        projected.key,
        CATALOG.serialize_unit(
            replace(producer, spec=TerraformDesiredUnit(source=replace(producer.spec.source, revision="d" * 40))),
            profile="desired",
        ),
    )
    with pytest.raises(ProjectionCompilerError, match="exact typed Unit"):
        TemplateResolutionSession.begin(CATALOG, InMemoryWorkspace(mutable=False)).record(producer, wrong_document)


def _artifact_fixture(
    *, source_revision: str = REVISION
) -> tuple[UnitResource[Any], ProjectedDocument, JsonObject, JsonObject]:
    producer = _desired_unit("oci-images")
    projected = _projected(producer)
    artifact: JsonObject = {
        "apiVersion": "artifact.gitopsctr.io/v1",
        "kind": "ContainerImages",
        "metadata": {"name": "containers"},
        "producer": {
            "apiVersion": producer.gvk.api_version,
            "kind": producer.gvk.kind,
            "name": producer.name,
            "qualifiedName": QUALIFIED,
            "driverVersion": producer.driver.version,
            "sourceRevision": source_revision,
            "inputHashVersion": 1,
            "inputHash": INPUT_HASH,
        },
        "images": {"application": {"uri": "registry.example/app@sha256:" + "e" * 64}},
    }
    raw = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode()
    descriptors: dict[str, JsonObject] = {
        "containers": {
            "apiVersion": "artifact.gitopsctr.io/v1",
            "kind": "ContainerImages",
            "path": "artifacts/application/producer/containers.json",
            "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "mediaType": "application/vnd.gitopsctr.container-images.v1+json",
        }
    }
    return producer, projected, _receipt(producer, projected, artifacts=descriptors), artifact


def _resolve_artifact(session: TemplateResolutionSession) -> TemplateResolution:
    return session.resolve(
        {
            "fromArtifact": {
                "unit": QUALIFIED,
                "name": "containers",
                "apiVersion": "artifact.gitopsctr.io/v1",
                "kind": "ContainerImages",
                "pointer": "/images/application/uri",
            }
        },
        "/inputs/image",
        unit=_desired_unit(),
        context=_context(),
    )


def test_artifact_resolution_authenticates_receipt_descriptor_bytes_and_producer() -> None:
    producer, projected, receipt, artifact = _artifact_fixture()
    observed = InMemoryWorkspace(
        (
            _entry("units/application/producer.json", receipt),
            _entry("artifacts/application/producer/containers.json", artifact),
        ),
        mutable=False,
    )
    resolved = _resolve_artifact(_session(observed, producer, projected))
    assert isinstance(resolved.value, str)
    assert resolved.value.startswith("registry.example/app@sha256:")
    assert resolved.artifacts[f"{QUALIFIED}/containers"].startswith("sha256:")


def test_artifact_resolution_blocks_stale_producer_and_rejects_tampered_bytes() -> None:
    producer, projected, receipt, stale_artifact = _artifact_fixture(source_revision="c" * 40)
    stale_observed = InMemoryWorkspace(
        (
            _entry("units/application/producer.json", receipt),
            _entry("artifacts/application/producer/containers.json", stale_artifact),
        ),
        mutable=False,
    )
    with pytest.raises(PendingTemplateReference, match="stale"):
        _resolve_artifact(_session(stale_observed, producer, projected))

    producer, projected, receipt, artifact = _artifact_fixture()
    artifact["images"] = {"application": {"uri": "tampered"}}
    tampered_observed = InMemoryWorkspace(
        (
            _entry("units/application/producer.json", receipt),
            _entry("artifacts/application/producer/containers.json", artifact),
        ),
        mutable=False,
    )
    with pytest.raises(ReferenceUnavailable, match="unauthenticated"):
        _resolve_artifact(_session(tampered_observed, producer, projected))


def test_cross_root_current_unit_resolves_only_with_exact_current_receipt_and_artifact() -> None:
    producer, projected, receipt, artifact = _artifact_fixture()
    desired_entry = _entry(projected.key, projected.mutable_document())
    current_workspace = InMemoryWorkspace((desired_entry,), mutable=False)
    current = {projected.identity: projected}
    observed = InMemoryWorkspace(
        (
            _entry("units/application/producer.json", receipt),
            _entry("artifacts/application/producer/containers.json", artifact),
        ),
        mutable=False,
    )
    session = TemplateResolutionSession.begin(CATALOG, observed, current, current_workspace)
    resolved = _resolve_artifact(session)
    assert isinstance(resolved.value, str)
    assert resolved.value.startswith("registry.example/app@sha256:")

    session.declare(QUALIFIED)
    with pytest.raises(PendingTemplateReference, match="pending projection"):
        _resolve_artifact(session)


def test_cross_root_current_unit_stale_missing_and_ambiguous_observations_are_terminal() -> None:
    producer = _desired_unit()
    projected = _projected(producer)
    current_workspace = InMemoryWorkspace((_entry(projected.key, projected.mutable_document()),), mutable=False)
    current = {projected.identity: projected}
    stale = _receipt(producer, projected, unit_content_id="sha256:" + "0" * 64)
    stale_session = TemplateResolutionSession.begin(
        CATALOG,
        InMemoryWorkspace((_entry("units/application/producer.json", stale),), mutable=False),
        current,
        current_workspace,
    )
    with pytest.raises(ReferenceUnavailable, match="stale for its current Unit") as stale_error:
        _resolve_receipt(stale_session, _desired_unit())
    assert not isinstance(stale_error.value, PendingTemplateReference)

    missing_session = TemplateResolutionSession.begin(
        CATALOG,
        InMemoryWorkspace(mutable=False),
        current,
        current_workspace,
    )
    with pytest.raises(ReferenceUnavailable, match="no observed evidence") as missing_error:
        _resolve_receipt(missing_session, _desired_unit())
    assert not isinstance(missing_error.value, PendingTemplateReference)

    valid = _receipt(producer, projected)
    ambiguous_session = TemplateResolutionSession.begin(
        CATALOG,
        InMemoryWorkspace(
            (
                _entry("units/application/producer.json", valid),
                WorkspaceEntry.file(
                    "units/application/producer.yaml",
                    b"apiVersion: gitopsctr.io/v1\nkind: Receipt\n",
                ),
            ),
            mutable=False,
        ),
        current,
        current_workspace,
    )
    with pytest.raises(ProjectionCompilerError, match="ambiguous"):
        _resolve_receipt(ambiguous_session, _desired_unit())
