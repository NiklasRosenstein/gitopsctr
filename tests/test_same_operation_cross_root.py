"""Same-operation root Unit evidence consumed by Stack projection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import cast

import pytest

from gitopsctr.adapters.filesystem.unit_projection import FilesystemUnitProjectionHost
from gitopsctr.adapters.git.source_lineage import GitSourceLineageEncoder
from gitopsctr.adapters.git.source_selection import GitUnitSourceSelector
from gitopsctr.application.apply import AuthoredChangeSet, _issue_authored_document
from gitopsctr.application.apply_compilers import (
    CatalogApplyDocumentValidator,
    CatalogLogicalUnitProjector,
    CatalogStackProjectionCompiler,
    CatalogUnitProjectionCompiler,
)
from gitopsctr.application.apply_projection import (
    ApplyProjectionContext,
    ApplyProjectionError,
    ApplyProjectionPolicy,
    CandidateTransformation,
    ExactPlane,
    FrozenAuthoredDocument,
    HmacRootIncarnationIssuer,
    ProjectedDocument,
    RetainedSourcePlane,
    SourceBindingRole,
    WorkspaceProjectionContext,
    _issue_retained_source_descriptor,
    project_apply,
)
from gitopsctr.application.model import (
    ChannelId,
    ContentId,
    EnvironmentId,
    HeadObservation,
    RetainedSourceHandle,
    RetentionStoreId,
    SnapshotId,
    SourceId,
    SourceSnapshotId,
    _issue_retained_source,
)
from gitopsctr.application.snapshots import SnapshotView
from gitopsctr.application.workspace import InMemoryWorkspace, WorkspaceEntry, entry_content_id
from gitopsctr.contracts import DesiredStackSpec
from gitopsctr.contrib.drivers.terraform import TerraformDesiredUnit
from gitopsctr.registry import DRIVER_GVKS, DRIVER_NAMES_BY_GVK, UNIT_DRIVERS
from gitopsctr.resource_api import JsonObject
from gitopsctr.resources import CORE_API_VERSION, ResourceCatalog

CATALOG = ResourceCatalog(UNIT_DRIVERS, DRIVER_NAMES_BY_GVK, DRIVER_GVKS)
REVISION = "a" * 40
MANIFEST_DIGEST = "sha256:" + "d" * 64
MANIFEST_V1 = b"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: producer-v1\n"
MANIFEST_V2 = b"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: producer-v2\n"


def _canonical(document: JsonObject) -> bytes:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _plane(channel: str, snapshot: str, workspace: InMemoryWorkspace) -> ExactPlane:
    snapshot_id = SnapshotId(snapshot)
    return ExactPlane(
        HeadObservation.present(ChannelId(channel), snapshot_id, f"{channel}-incarnation"),
        workspace,
        SnapshotView(snapshot_id, workspace.content_id, workspace),
    )


def _source() -> RetainedSourcePlane:
    workspace = InMemoryWorkspace(
        (
            WorkspaceEntry.file("gitopsctr.yaml", b"kind: Project\n"),
            WorkspaceEntry.file("producer-v1/manifest.yaml", MANIFEST_V1, executable=True),
            WorkspaceEntry.file("producer-v2/manifest.yaml", MANIFEST_V2),
            WorkspaceEntry.file("consumer/main.tf", b"terraform {}\n"),
        ),
        mutable=False,
    )
    snapshot = SnapshotId(f"git-source:{REVISION}")
    retained = _issue_retained_source(
        RetainedSourceHandle("same-operation"),
        RetentionStoreId("same-operation-store"),
        SourceSnapshotId(SourceId("same-operation-source"), snapshot),
        workspace.content_id,
    )
    descriptors = tuple(
        _issue_retained_source_descriptor(
            retained,
            binding,
            role,
            key,
            ContentId(f"sha256:{selector * 64}"),
        )
        for binding, role, key, selector in (
            ("authored", SourceBindingRole.PRIMARY_AUTHORED, "gitopsctr.yaml", "1"),
            ("producer", SourceBindingRole.WORKLOAD, "producer-v1/manifest.yaml", "2"),
            ("consumer", SourceBindingRole.WORKLOAD, "consumer/main.tf", "3"),
        )
    )
    return RetainedSourcePlane(
        retained,
        _plane("source", snapshot.value, workspace),
        descriptors,
    )


def _context(source: RetainedSourcePlane) -> ApplyProjectionContext:
    return ApplyProjectionContext(
        EnvironmentId("dev"),
        ChannelId("desired/dev"),
        ChannelId("observed/dev"),
        ChannelId("candidate/dev"),
        ApplyProjectionPolicy(),
        projection_context=WorkspaceProjectionContext(b"kind: Project\n", b"kind: Environment\n"),
        primary_source=source.descriptors[0],
        named_sources=source.descriptors[1:],
        root_identity_issuer=HmacRootIncarnationIssuer("same-operation", "same-operation-seed"),
    )


def _producer(path: str = "producer-v1") -> JsonObject:
    return {
        "apiVersion": "unit.gitopsctr.io/v1",
        "kind": "KubernetesManifests",
        "metadata": {"name": "producer"},
        "spec": {
            "source": {"path": path, "revision": REVISION, "inputs": ["*.yaml"]},
            "materialize": {"type": "plain", "paths": ["*.yaml"]},
            "delivery": {"mode": "external"},
        },
    }


def _template() -> JsonObject:
    return {
        "apiVersion": CORE_API_VERSION,
        "kind": "StackTemplate",
        "metadata": {"name": "consumer-template"},
        "spec": {
            "parameters": [],
            "unitTemplates": {
                "deploy": {
                    "apiVersion": "unit.gitopsctr.io/v1",
                    "kind": "Terraform",
                    "spec": {
                        "source": {"path": "consumer", "revision": REVISION, "inputs": ["main.tf"]},
                        "terraform": {
                            "backend": {},
                            "variables": {
                                "manifestDigest": {
                                    "fromReceipt": {
                                        "unit": "producer",
                                        "pointer": "/applied/manifestDigest",
                                    }
                                }
                            },
                            "observeOutputs": [],
                            "checks": [],
                        },
                    },
                }
            },
        },
    }


def _stack() -> JsonObject:
    return {
        "apiVersion": CORE_API_VERSION,
        "kind": "Stack",
        "metadata": {"name": "consumer"},
        "spec": {"template": "consumer-template"},
    }


def _compilers(source: RetainedSourcePlane) -> tuple[CatalogUnitProjectionCompiler, CatalogStackProjectionCompiler]:
    encoder = GitSourceLineageEncoder({SourceId("same-operation-source"): "https://example.test/source.git"})
    logical = CatalogLogicalUnitProjector(
        CATALOG,
        encoder,
        FilesystemUnitProjectionHost(CATALOG),
        source_selector=GitUnitSourceSelector(
            encoder,
            {"producer": "producer", "consumer/deploy": "consumer"},
        ),
    )
    return CatalogUnitProjectionCompiler(CATALOG, logical), CatalogStackProjectionCompiler(
        CATALOG, logical, source_encoder=encoder
    )


def _producer_projection(
    producer: JsonObject,
    source: RetainedSourcePlane,
    context: ApplyProjectionContext,
    *,
    current_workspace: InMemoryWorkspace | None = None,
    current_document: ProjectedDocument | None = None,
) -> CandidateTransformation:
    unit_compiler, _ = _compilers(source)
    current = {} if current_document is None else {current_document.identity: current_document}
    return unit_compiler.project(
        (FrozenAuthoredDocument.from_change("producer", ContentId("sha256:" + "4" * 64), producer),),
        current,
        current_workspace or InMemoryWorkspace(mutable=False),
        (source,),
        InMemoryWorkspace(mutable=False),
        context,
    )


def _desired_workspace(transformation: CandidateTransformation) -> InMemoryWorkspace:
    projected = transformation.writes[0]
    entries = [WorkspaceEntry.file(projected.key, _canonical(projected.mutable_document()))]
    for replacement in transformation.payload_replacements:
        entries.extend(replacement.entries)
    entries.extend(transformation.payload_writes)
    return InMemoryWorkspace(tuple(entries), mutable=False)


def _receipt(projected: ProjectedDocument) -> WorkspaceEntry:
    unit = CATALOG.parse_unit(projected.mutable_document(), profile="desired")
    raw = _canonical(projected.mutable_document())
    document: JsonObject = {
        "apiVersion": CORE_API_VERSION,
        "kind": "Receipt",
        "metadata": {"name": "producer"},
        "spec": {
            "subject": {
                "apiVersion": unit.gvk.api_version,
                "kind": unit.gvk.kind,
                "name": "producer",
                "qualifiedName": "producer",
            },
            "desired": {"unitContentId": entry_content_id(WorkspaceEntry.file(projected.key, raw)).value},
        },
        "status": {
            "controller": {},
            "result": {"applied": {"manifestDigest": MANIFEST_DIGEST, "inventory": []}},
        },
    }
    return WorkspaceEntry.file("units/producer.json", _canonical(document))


def _changes(producer: JsonObject) -> AuthoredChangeSet:
    source_snapshot = SourceSnapshotId(SourceId("same-operation-source"), SnapshotId(f"git-source:{REVISION}"))
    return AuthoredChangeSet(
        (
            _issue_authored_document("producer", producer, ContentId("sha256:" + "4" * 64)),
            _issue_authored_document("template", _template(), ContentId("sha256:" + "5" * 64)),
            _issue_authored_document("stack", _stack(), ContentId("sha256:" + "6" * 64)),
        ),
        source_snapshot,
    )


def _apply(
    producer: JsonObject,
    source: RetainedSourcePlane,
    context: ApplyProjectionContext,
    current: InMemoryWorkspace,
    observed: InMemoryWorkspace,
    *,
    stack_compiler: object | None = None,
):
    unit_compiler, real_stack_compiler = _compilers(source)
    return project_apply(
        _changes(producer),
        current_desired=_plane("desired/dev", "desired-current", current),
        observed=_plane("observed/dev", "observed-current", observed),
        retained_sources=(source,),
        context=context,
        validator=CatalogApplyDocumentValidator(CATALOG),
        unit_compiler=unit_compiler,
        stack_compiler=cast(CatalogStackProjectionCompiler, stack_compiler or real_stack_compiler),
    )


def test_new_root_unit_is_exactly_visible_to_same_operation_stack() -> None:
    source = _source()
    context = _context(source)
    expected = _producer_projection(_producer(), source, context)
    projected = expected.writes[0]
    result = _apply(
        _producer(),
        source,
        context,
        InMemoryWorkspace(mutable=False),
        InMemoryWorkspace((_receipt(projected),), mutable=False),
    )

    candidate_entries = {entry.key: entry for entry in result.candidate.list_entries()}
    expected_payload = expected.payload_replacements[0].entries[0]
    actual_payload = candidate_entries["materialized/producer/manifest.yaml"]
    assert result.candidate.read("units/producer.json") == _canonical(projected.mutable_document())
    assert entry_content_id(candidate_entries["units/producer.json"]) == entry_content_id(
        WorkspaceEntry.file(projected.key, _canonical(projected.mutable_document()))
    )
    assert actual_payload.content == expected_payload.content == MANIFEST_V1
    assert actual_payload.executable is expected_payload.executable is True
    assert entry_content_id(actual_payload) == entry_content_id(expected_payload)

    deploy = CATALOG.parse_unit(json.loads(result.candidate.read("units/consumer/deploy.json")), profile="desired")
    assert isinstance(deploy.spec, TerraformDesiredUnit)
    assert deploy.spec.terraform is not None and deploy.spec.terraform.variables is not None
    assert deploy.spec.terraform.variables["manifestDigest"] == MANIFEST_DIGEST
    assert deploy.spec.resolvedInputs is not None
    assert deploy.spec.resolvedInputs.receipts == {
        "producer": "sha256:" + hashlib.sha256(_receipt(projected).content or b"").hexdigest()
    }
    stack = CATALOG.parse_stack(json.loads(result.candidate.read("stacks/consumer.json")), profile="desired")
    assert isinstance(stack.spec, DesiredStackSpec) and stack.spec.activeProjection is not None


def test_changed_root_unit_blocks_the_consumer_of_its_old_receipt() -> None:
    source = _source()
    context = _context(source)
    old = _producer_projection(_producer(), source, context)
    current = _desired_workspace(old)

    result = _apply(
        _producer("producer-v2"),
        source,
        context,
        current,
        InMemoryWorkspace((_receipt(old.writes[0]),), mutable=False),
    )

    assert not any(entry.key == "units/consumer/deploy.json" for entry in result.candidate.list_entries())
    blocks = json.loads(result.candidate.read(".gitopsctr/transition-blocks.json"))["blocks"]
    assert blocks == {"consumer/deploy": "receipt producer 'producer' is stale for its current Unit"}


def test_unchanged_root_unit_resolves_its_exact_current_receipt() -> None:
    source = _source()
    context = _context(source)
    current_projection = _producer_projection(_producer(), source, context)
    current = _desired_workspace(current_projection)

    result = _apply(
        _producer(),
        source,
        context,
        current,
        InMemoryWorkspace((_receipt(current_projection.writes[0]),), mutable=False),
    )

    assert result.candidate.read("units/producer.json") == current.read("units/producer.json")
    assert result.candidate.read("materialized/producer/manifest.yaml") == MANIFEST_V1
    deploy = CATALOG.parse_unit(json.loads(result.candidate.read("units/consumer/deploy.json")), profile="desired")
    assert isinstance(deploy.spec, TerraformDesiredUnit)
    assert deploy.spec.terraform is not None and deploy.spec.terraform.variables is not None
    assert deploy.spec.terraform.variables["manifestDigest"] == MANIFEST_DIGEST


@dataclass(frozen=True)
class _OverwritingStackCompiler:
    delegate: CatalogStackProjectionCompiler
    replacement: ProjectedDocument

    def project(self, *args, **kwargs) -> CandidateTransformation:  # type: ignore[no-untyped-def]
        projected = self.delegate.project(*args, **kwargs)
        return CandidateTransformation(
            (*projected.writes, self.replacement),
            deletes=projected.deletes,
            payload_writes=projected.payload_writes,
            payload_deletes=projected.payload_deletes,
            payload_prefixes=projected.payload_prefixes,
            payload_replacements=projected.payload_replacements,
        )


def test_stack_transformation_cannot_overwrite_the_intermediate_root_unit() -> None:
    source = _source()
    context = _context(source)
    expected = _producer_projection(_producer(), source, context)
    _, stack_compiler = _compilers(source)
    root = expected.writes[0]
    replacement_document = root.mutable_document()
    cast(dict[str, object], replacement_document["metadata"])["uid"] = "d1-forged"

    with pytest.raises(ApplyProjectionError, match="unrelated desired root"):
        _apply(
            _producer(),
            source,
            context,
            InMemoryWorkspace(mutable=False),
            InMemoryWorkspace((_receipt(root),), mutable=False),
            stack_compiler=_OverwritingStackCompiler(
                stack_compiler,
                ProjectedDocument("units/producer.json", replacement_document),
            ),
        )
