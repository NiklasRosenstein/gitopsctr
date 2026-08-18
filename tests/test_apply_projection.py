"""Backend-neutral characterization tests for pure apply projection."""

from __future__ import annotations

import hashlib
import json
from typing import cast

import pytest
import yaml

from gitopsctr.application.apply import AuthoredChangeSet, _issue_authored_document
from gitopsctr.application.apply_compilers import (
    ArtifactImportRequest,
    ArtifactImportResolution,
    PromotionLineage,
)
from gitopsctr.application.apply_projection import (
    ApplyDocumentValidator,
    ApplyProjectionContext,
    ApplyProjectionError,
    ApplyProjectionPolicy,
    ApplyPublicationDecision,
    CandidateTransformation,
    ExactPlane,
    FinalizedTombstone,
    HmacRootIncarnationIssuer,
    PayloadPrefixReplacement,
    ProjectedDocument,
    RetainedSourcePlane,
    RootIdentityRequest,
    SourceBindingRole,
    WorkspaceProjectionContext,
    _issue_promotion_source_descriptor,
    _issue_retained_source_descriptor,
    _issue_root_identity,
    payload_prefix_evidence,
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
from gitopsctr.application.workspace import ImmutableWorkspace, InMemoryWorkspace, WorkspaceEntry
from gitopsctr.contracts import ArtifactImport, PromotionStackReference, ResolvedArtifactImport, StackSpec
from gitopsctr.document import JsonObjectValue
from gitopsctr.resource_api import GVK, JsonObject
from gitopsctr.resources import ResourceMetadata, StackResource


def _document(name: str, *, kind: str = "Terraform") -> JsonObject:
    return {
        "apiVersion": "gitopsctr.io/v1" if kind in {"Stack", "StackTemplate"} else "unit.gitopsctr.io/v1",
        "kind": kind,
        "metadata": {"name": name},
        "spec": {"source": {"path": "."}} if kind not in {"Stack", "StackTemplate"} else {},
    }


def _changes(*documents: JsonObject) -> AuthoredChangeSet:
    return AuthoredChangeSet(
        tuple(
            _issue_authored_document(f"input-{index}", document, ContentId(f"sha256:{index:064x}"))
            for index, document in enumerate(documents, start=1)
        )
    )


def _workspace(*entries: WorkspaceEntry) -> InMemoryWorkspace:
    return InMemoryWorkspace(entries, mutable=False)


def _plane(channel: str, workspace: ImmutableWorkspace, snapshot: str) -> ExactPlane:
    snapshot_id = SnapshotId(snapshot)
    return ExactPlane(
        HeadObservation.present(ChannelId(channel), snapshot_id, f"{snapshot}-incarnation"),
        workspace,
        SnapshotView(snapshot_id, workspace.content_id, workspace),
    )


def _desired(workspace: ImmutableWorkspace | None = None) -> ExactPlane:
    return _plane("desired/dev", workspace or _workspace(), "desired-1")


def _observed(workspace: ImmutableWorkspace | None = None) -> ExactPlane:
    return _plane("observed/dev", workspace or _workspace(), "observed-1")


def _source_plane(retained, workspace: ImmutableWorkspace) -> RetainedSourcePlane:  # type: ignore[no-untyped-def]
    snapshot = retained.source_snapshot_id.snapshot_id
    exact = ExactPlane(
        HeadObservation.present(ChannelId("source/default"), snapshot, f"{snapshot}-incarnation"),
        workspace,
        SnapshotView(snapshot, workspace.content_id, workspace),
    )
    descriptor = _issue_retained_source_descriptor(
        retained,
        "primary",
        SourceBindingRole.PRIMARY_AUTHORED,
        "inputs",
        ContentId("sha256:" + "f" * 64),
    )
    return RetainedSourcePlane(retained, exact, (descriptor,))


class _Validator(ApplyDocumentValidator):
    def validate_authored(self, document: JsonObject) -> None:
        assert document["kind"]

    def validate_desired(self, document: JsonObject) -> None:
        assert document["metadata"]

    def validate_graph(self, documents):  # type: ignore[no-untyped-def]
        assert len(documents) == len(set(documents))

    def validate_workspace(self, workspace: ImmutableWorkspace) -> None:
        assert not workspace.is_mutable


VALIDATOR = _Validator()


class _RootIssuer:
    issuer_id = "test-root-issuer"

    def issue(self, request: RootIdentityRequest):  # type: ignore[no-untyped-def]
        evidence = "\0".join(
            (
                request.environment_id.value,
                request.api_version,
                request.kind,
                request.qualified_name,
                request.source_snapshot_id.to_wire() if request.source_snapshot_id else "",
                request.authored_content_id.value,
                *request.finalized_tombstone_uids,
            )
        )
        uid = f"d1-{hashlib.sha256(evidence.encode()).hexdigest()[:32]}"
        return _issue_root_identity(request, self.issuer_id, uid)


ROOT_ISSUER = _RootIssuer()


def _context(*, partition: str | None = None, dry_run: bool = False, review: bool = False) -> ApplyProjectionContext:
    return ApplyProjectionContext(
        EnvironmentId("dev"),
        ChannelId("desired/dev"),
        ChannelId("observed/dev"),
        ChannelId("candidate/dev"),
        ApplyProjectionPolicy(review_required=review),
        partition,
        dry_run,
        root_identity_issuer=ROOT_ISSUER,
    )


def _json(workspace: ImmutableWorkspace, key: str) -> JsonObject:
    return cast(JsonObject, json.loads(workspace.read(key)))


def _metadata(document: JsonObject) -> JsonObject:
    value = document["metadata"]
    assert isinstance(value, dict)
    return value


def test_unit_projection_is_deterministic_and_uses_logical_content_identity() -> None:
    current = _workspace()
    result = project_apply(
        _changes(_document("application")),
        current_desired=_desired(current),
        observed=_observed(),
        context=_context(partition="application"),
        validator=VALIDATOR,
    )

    document = _json(result.candidate, "units/application.json")
    metadata = _metadata(document)
    assert metadata == {
        "labels": {"gitopsctr.io/partition": "application"},
        "name": "application",
        "uid": metadata["uid"],
    }
    assert isinstance(metadata["uid"], str) and metadata["uid"].startswith("d1-")
    assert result.plan.decision is ApplyPublicationDecision.DIRECT
    assert result.plan.candidate_content_id == result.candidate.content_id

    repeated = project_apply(
        _changes(_document("application")),
        current_desired=_desired(current),
        observed=_observed(),
        context=_context(partition="application"),
        validator=VALIDATOR,
    )
    assert repeated.candidate.content_id == result.candidate.content_id


def test_production_root_issuer_is_tombstone_fenced_and_permutation_invariant() -> None:
    issuer = HmacRootIncarnationIssuer("memory-root-issuer", "explicit-memory-identity-seed")
    tombstones = (
        FinalizedTombstone("unit.gitopsctr.io/v1", "Terraform", "application", "d1-old-b"),
        FinalizedTombstone("unit.gitopsctr.io/v1", "Terraform", "application", "d1-old-a"),
    )
    context = ApplyProjectionContext(
        EnvironmentId("dev"),
        ChannelId("desired/dev"),
        ChannelId("observed/dev"),
        ChannelId("candidate/dev"),
        ApplyProjectionPolicy(),
        root_identity_issuer=issuer,
        finalized_tombstones=tombstones,
    )
    first = project_apply(
        _changes(_document("application")),
        current_desired=_desired(),
        observed=_observed(),
        context=context,
        validator=VALIDATOR,
    )
    uid = _metadata(_json(first.candidate, "units/application.json"))["uid"]
    assert uid not in {"d1-old-a", "d1-old-b"}
    repeated = project_apply(
        _changes(_document("application")),
        current_desired=_desired(),
        observed=_observed(),
        context=context,
        validator=VALIDATOR,
    )
    assert _metadata(_json(repeated.candidate, "units/application.json"))["uid"] == uid
    request = RootIdentityRequest(
        EnvironmentId("dev"),
        "unit.gitopsctr.io/v1",
        "Terraform",
        "application",
        None,
        ContentId("sha256:" + "a" * 64),
        ("d1-old-b", "d1-old-a"),
    )
    assert request.finalized_tombstone_uids == ("d1-old-a", "d1-old-b")


def test_root_issuer_rejects_tampered_or_foreign_issued_identity() -> None:
    request = RootIdentityRequest(
        EnvironmentId("dev"),
        "unit.gitopsctr.io/v1",
        "Terraform",
        "application",
        None,
        ContentId("sha256:" + "a" * 64),
    )
    issued = _issue_root_identity(request, "issuer-a", "d1-issued")
    object.__setattr__(issued, "uid", "d1-tampered")
    with pytest.raises(TypeError, match="modified after issuance"):
        issued._validate()

    class ForeignIssuer:
        issuer_id = "issuer-b"

        def issue(self, request: RootIdentityRequest):  # type: ignore[no-untyped-def]
            return _issue_root_identity(request, "issuer-a", "d1-issued")

    context = ApplyProjectionContext(
        EnvironmentId("dev"),
        ChannelId("desired/dev"),
        ChannelId("observed/dev"),
        ChannelId("candidate/dev"),
        ApplyProjectionPolicy(),
        root_identity_issuer=ForeignIssuer(),
    )
    with pytest.raises(ApplyProjectionError, match="does not bind this issuer"):
        project_apply(
            _changes(_document("application")),
            current_desired=_desired(),
            observed=_observed(),
            context=context,
            validator=VALIDATOR,
        )


def test_new_root_fails_closed_without_an_issued_identity_provider() -> None:
    with pytest.raises(ApplyProjectionError, match="RootIncarnationIssuer"):
        project_apply(
            _changes(_document("application")),
            current_desired=_desired(),
            observed=_observed(),
            context=ApplyProjectionContext(
                EnvironmentId("dev"),
                ChannelId("desired/dev"),
                ChannelId("observed/dev"),
                ChannelId("candidate/dev"),
                ApplyProjectionPolicy(),
            ),
            validator=VALIDATOR,
        )


def test_noop_preserves_workspace_content_identity_and_existing_partition() -> None:
    first = project_apply(
        _changes(_document("application")),
        current_desired=_desired(),
        observed=_observed(),
        context=_context(partition="application"),
        validator=VALIDATOR,
    )
    current = InMemoryWorkspace(first.candidate.list_entries(), mutable=False)

    second = project_apply(
        _changes(_document("application")),
        current_desired=_desired(current),
        observed=_observed(),
        context=_context(dry_run=True),
        validator=VALIDATOR,
    )

    assert second.plan.decision is ApplyPublicationDecision.NO_CHANGE
    assert second.plan.base_content_id == second.plan.candidate_content_id
    assert _metadata(_json(second.candidate, "units/application.json"))["labels"] == {
        "gitopsctr.io/partition": "application"
    }


def test_noop_preserves_noncanonical_yaml_bytes_key_and_mode() -> None:
    first = project_apply(
        _changes(_document("application")),
        current_desired=_desired(),
        observed=_observed(),
        context=_context(),
        validator=VALIDATOR,
    )
    desired = _json(first.candidate, "units/application.json")
    yaml_bytes = b"# intentionally noncanonical desired bytes\n" + yaml.safe_dump(desired, sort_keys=False).encode()
    current = _workspace(WorkspaceEntry.file("units/application.yaml", yaml_bytes, executable=True))

    result = project_apply(
        _changes(_document("application")),
        current_desired=_desired(current),
        observed=_observed(),
        context=_context(),
        validator=VALIDATOR,
    )

    assert result.plan.decision is ApplyPublicationDecision.NO_CHANGE
    assert result.candidate.content_id == current.content_id
    assert result.candidate.inspect("units/application.yaml").executable is True
    assert result.candidate.read("units/application.yaml") == yaml_bytes


def test_partition_pruning_marks_omitted_root_for_deterministic_deletion() -> None:
    first = project_apply(
        _changes(_document("first"), _document("second")),
        current_desired=_desired(),
        observed=_observed(),
        context=_context(partition="application"),
        validator=VALIDATOR,
    )
    current = InMemoryWorkspace(first.candidate.list_entries(), mutable=False)

    next_result = project_apply(
        _changes(_document("second")),
        current_desired=_desired(current),
        observed=_observed(),
        context=_context(partition="application"),
        validator=VALIDATOR,
    )

    omitted = _json(next_result.candidate, "units/first.json")
    deletion = _metadata(omitted)["deletion"]
    assert isinstance(deletion, dict)
    assert deletion["generation"] == 1
    assert _metadata(omitted)["labels"] == {"gitopsctr.io/partition": "application"}


def test_authoritative_empty_partition_prunes_the_full_owned_closure() -> None:
    root = _document("root", kind="Stack")
    root["metadata"] = {
        "name": "root",
        "uid": "d1-root",
        "labels": {"gitopsctr.io/partition": "application"},
    }
    child = _document("child")
    child["metadata"] = {
        "name": "child",
        "uid": "d1-child",
        "ownerReferences": [{"apiVersion": root["apiVersion"], "kind": root["kind"], "name": "root", "uid": "d1-root"}],
    }
    current = _workspace(
        WorkspaceEntry.file("stacks/root.json", json.dumps(root).encode()),
        WorkspaceEntry.file("units/root/child.json", json.dumps(child).encode()),
    )

    result = project_apply(
        AuthoredChangeSet(()),
        current_desired=_desired(current),
        observed=_observed(),
        context=_context(partition="application"),
        validator=VALIDATOR,
    )

    assert _metadata(_json(result.candidate, "stacks/root.json"))["deletion"]
    assert _metadata(_json(result.candidate, "units/root/child.json"))["deletion"]


def test_stack_owned_leaf_names_are_qualified_by_owner_storage_path() -> None:
    stacks = []
    for stack_name in ("alpha", "beta"):
        stack = _document(stack_name, kind="Stack")
        stack["metadata"] = {"name": stack_name, "uid": f"d1-{stack_name}"}
        child = _document("db")
        child["metadata"] = {
            "name": "db",
            "uid": f"d1-{stack_name}-db",
            "ownerReferences": [
                {"apiVersion": stack["apiVersion"], "kind": "Stack", "name": stack_name, "uid": f"d1-{stack_name}"}
            ],
        }
        stacks.extend(
            (
                WorkspaceEntry.file(f"stacks/{stack_name}.json", json.dumps(stack).encode()),
                WorkspaceEntry.file(f"units/{stack_name}/db.json", json.dumps(child).encode()),
            )
        )
    current = _workspace(*stacks)

    result = project_apply(
        _changes(_document("application")),
        current_desired=_desired(current),
        observed=_observed(),
        context=_context(),
        validator=VALIDATOR,
    )
    assert result.candidate.read("units/alpha/db.json")
    assert result.candidate.read("units/beta/db.json")

    spoofed = _workspace(
        *(entry for entry in stacks if entry.key != "units/beta/db.json"),
        WorkspaceEntry.file(
            "units/beta/db.json",
            json.dumps(
                {
                    **_json(current, "units/alpha/db.json"),
                }
            ).encode(),
        ),
    )
    with pytest.raises(ApplyProjectionError, match="canonical GVK/name"):
        project_apply(
            _changes(_document("application")),
            current_desired=_desired(spoofed),
            observed=_observed(),
            context=_context(),
            validator=VALIDATOR,
        )


def test_empty_unpartitioned_apply_and_desired_symlink_fail_closed() -> None:
    with pytest.raises(ApplyProjectionError, match="zero documents"):
        project_apply(
            AuthoredChangeSet(()),
            current_desired=_desired(),
            observed=_observed(),
            context=_context(),
            validator=VALIDATOR,
        )


def test_exact_plane_rejects_a_head_snapshot_mismatch() -> None:
    workspace = _workspace(WorkspaceEntry.file("state.json", b"{}"))
    with pytest.raises(ApplyProjectionError, match="same content"):
        ExactPlane(
            HeadObservation.present(ChannelId("desired/dev"), SnapshotId("other"), "incarnation"),
            workspace,
            SnapshotView(SnapshotId("actual"), workspace.content_id, workspace),
        )


def test_authored_partition_label_cannot_self_assign_a_root() -> None:
    authored = _document("application")
    authored["metadata"] = {"name": "application", "labels": {"gitopsctr.io/partition": "application"}}
    with pytest.raises(ApplyProjectionError, match="authored partition labels"):
        project_apply(
            _changes(authored),
            current_desired=_desired(),
            observed=_observed(),
            context=_context(),
            validator=VALIDATOR,
        )
    permitted = project_apply(
        _changes(authored),
        current_desired=_desired(),
        observed=_observed(),
        context=_context(partition="application"),
        validator=VALIDATOR,
    )
    assert _metadata(_json(permitted.candidate, "units/application.json"))["labels"] == {
        "gitopsctr.io/partition": "application"
    }
    with pytest.raises(ApplyProjectionError, match="symbolic links"):
        project_apply(
            _changes(_document("application")),
            current_desired=_desired(_workspace(WorkspaceEntry.symlink("units/escape", "outside"))),
            observed=_observed(),
            context=_context(),
            validator=VALIDATOR,
        )


def test_cross_partition_transfer_and_mutable_plane_are_rejected() -> None:
    first = project_apply(
        _changes(_document("application")),
        current_desired=_desired(),
        observed=_observed(),
        context=_context(partition="application"),
        validator=VALIDATOR,
    )
    current = InMemoryWorkspace(first.candidate.list_entries(), mutable=False)
    with pytest.raises(ApplyProjectionError, match="belongs to partition"):
        project_apply(
            _changes(_document("application")),
            current_desired=_desired(current),
            observed=_observed(),
            context=_context(partition="other"),
            validator=VALIDATOR,
        )
    with pytest.raises(ApplyProjectionError, match="plane workspace must be immutable"):
        ExactPlane(HeadObservation.absent(ChannelId("observed/dev"), "observed-absent"), InMemoryWorkspace())


def test_stack_projection_is_an_explicit_pure_capability() -> None:
    class Compiler:
        def project(self, documents, current_desired, current_workspace, retained_sources, observed, context):  # type: ignore[no-untyped-def]
            assert documents[0].document["kind"] == "Stack"
            desired = _document("web", kind="Stack")
            desired["metadata"] = {"name": "web", "uid": "d1-web"}
            return CandidateTransformation((ProjectedDocument("stacks/web.json", desired),))

    with pytest.raises(ApplyProjectionError, match="StackProjectionCompiler"):
        project_apply(
            _changes(_document("web", kind="Stack")),
            current_desired=_desired(),
            observed=_observed(),
            context=_context(),
            validator=VALIDATOR,
        )

    result = project_apply(
        _changes(_document("web", kind="Stack")),
        current_desired=_desired(),
        observed=_observed(),
        context=_context(review=True, dry_run=True),
        validator=VALIDATOR,
        stack_compiler=Compiler(),
    )
    assert _metadata(_json(result.candidate, "stacks/web.json"))["uid"] == "d1-web"
    assert result.plan.decision is ApplyPublicationDecision.DRY_RUN


def test_stack_compiler_empty_or_unrelated_root_transformation_fails_closed() -> None:
    class EmptyCompiler:
        def project(self, documents, current_desired, current_workspace, retained_sources, observed, context):  # type: ignore[no-untyped-def]
            return CandidateTransformation(())

    class UnrelatedCompiler:
        def project(self, documents, current_desired, current_workspace, retained_sources, observed, context):  # type: ignore[no-untyped-def]
            unrelated = _document("unrelated", kind="Stack")
            unrelated["metadata"] = {"name": "unrelated", "uid": "d1-unrelated"}
            return CandidateTransformation((ProjectedDocument("stacks/unrelated.json", unrelated),))

    for compiler, match in ((EmptyCompiler(), "non-empty"), (UnrelatedCompiler(), "unrelated")):
        with pytest.raises(ApplyProjectionError, match=match):
            project_apply(
                _changes(_document("web", kind="Stack")),
                current_desired=_desired(),
                observed=_observed(),
                context=_context(),
                validator=VALIDATOR,
                stack_compiler=compiler,
            )


def test_stack_delta_preserves_non_document_payload_entry_identity() -> None:
    class Compiler:
        def project(self, documents, current_desired, current_workspace, retained_sources, observed, context):  # type: ignore[no-untyped-def]
            stack = _document("web", kind="Stack")
            stack["metadata"] = {"name": "web", "uid": "d1-web"}
            unit = _document("db")
            unit["metadata"] = {
                "name": "db",
                "uid": "d1-web-db",
                "ownerReferences": [
                    {"apiVersion": stack["apiVersion"], "kind": "Stack", "name": "web", "uid": "d1-web"}
                ],
            }
            return CandidateTransformation(
                (ProjectedDocument("stacks/web.json", stack), ProjectedDocument("units/web/db.json", unit)),
                payload_writes=(
                    WorkspaceEntry.file(".gitopsctr/projection-contexts/web.json", b"exact-context", executable=True),
                    WorkspaceEntry.file("materialized/web/db/state.json", b"exact-state"),
                ),
                payload_prefixes=(".gitopsctr/projection-contexts", "materialized/web/db"),
            )

    result = project_apply(
        _changes(_document("web", kind="Stack")),
        current_desired=_desired(),
        observed=_observed(),
        context=_context(),
        validator=VALIDATOR,
        stack_compiler=Compiler(),
    )
    entry = result.candidate.inspect(".gitopsctr/projection-contexts/web.json")
    assert entry.content == b"exact-context" and entry.executable is True
    assert result.candidate.read("materialized/web/db/state.json") == b"exact-state"


def test_stack_payload_scope_rejects_materialized_sibling_or_ancestor_prefixes() -> None:
    class Compiler:
        def project(self, documents, current_desired, current_workspace, retained_sources, observed, context):  # type: ignore[no-untyped-def]
            stack = _document("web", kind="Stack")
            stack["metadata"] = {"name": "web", "uid": "d1-web"}
            unit = _document("db")
            unit["metadata"] = {
                "name": "db",
                "uid": "d1-web-db",
                "ownerReferences": [
                    {"apiVersion": stack["apiVersion"], "kind": "Stack", "name": "web", "uid": "d1-web"}
                ],
            }
            return CandidateTransformation(
                (ProjectedDocument("stacks/web.json", stack), ProjectedDocument("units/web/db.json", unit)),
                payload_writes=(WorkspaceEntry.file("materialized/web/other/state.json", b"forbidden"),),
                payload_prefixes=("materialized/web",),
            )

    with pytest.raises(ApplyProjectionError, match="emitted resource identities"):
        project_apply(
            _changes(_document("web", kind="Stack")),
            current_desired=_desired(),
            observed=_observed(),
            context=_context(),
            validator=VALIDATOR,
            stack_compiler=Compiler(),
        )


def test_payload_delta_cannot_recursively_target_reserved_resource_roots() -> None:
    with pytest.raises(ApplyProjectionError, match="reserved resource roots"):
        CandidateTransformation((), payload_deletes=("units",), payload_prefixes=("units",))


def test_payload_replacement_closed_value_rejects_malformed_or_out_of_scope_evidence() -> None:
    content = ContentId("sha256:" + "a" * 64)
    entry = WorkspaceEntry.file("materialized/web/state.json", b"state")
    cases = (
        (
            TypeError,
            "expected_current_content_id",
            lambda: PayloadPrefixReplacement("materialized/web", cast(ContentId, "bad"), ()),
        ),
        (
            TypeError,
            "expected_current_entries",
            lambda: PayloadPrefixReplacement(
                "materialized/web", content, cast(tuple[tuple[str, ContentId], ...], (("bad",),))
            ),
        ),
        (
            ApplyProjectionError,
            "sorted and unique",
            lambda: PayloadPrefixReplacement(
                "materialized/web",
                content,
                (("materialized/web/b", content), ("materialized/web/a", content)),
            ),
        ),
        (
            ApplyProjectionError,
            "remain below",
            lambda: PayloadPrefixReplacement("materialized/web", content, (("materialized/other", content),)),
        ),
        (
            TypeError,
            "entries must",
            lambda: PayloadPrefixReplacement(
                "materialized/web", content, (), cast(tuple[WorkspaceEntry, ...], ("bad",))
            ),
        ),
        (
            ApplyProjectionError,
            "cannot repeat",
            lambda: PayloadPrefixReplacement("materialized/web", content, (), (entry, entry)),
        ),
        (
            ApplyProjectionError,
            "remain below",
            lambda: PayloadPrefixReplacement(
                "materialized/web", content, (), (WorkspaceEntry.file("materialized/other/state", b"state"),)
            ),
        ),
    )
    for error, message, construct in cases:
        with pytest.raises(error, match=message):
            construct()


def test_candidate_transformation_rejects_ambiguous_payload_deltas() -> None:
    replacement = PayloadPrefixReplacement("materialized/web", ContentId("sha256:" + "a" * 64), ())
    cases = (
        (
            TypeError,
            "writes must",
            lambda: CandidateTransformation(cast(tuple[ProjectedDocument, ...], "bad")),
        ),
        (
            TypeError,
            "deletes must",
            lambda: CandidateTransformation((), deletes=cast(tuple[str, ...], (1,))),
        ),
        (
            TypeError,
            "payload writes must",
            lambda: CandidateTransformation((), payload_writes=cast(tuple[WorkspaceEntry, ...], ("bad",))),
        ),
        (
            TypeError,
            "payload deletes must",
            lambda: CandidateTransformation((), payload_deletes=cast(tuple[str, ...], (1,))),
        ),
        (
            TypeError,
            "payload prefixes must",
            lambda: CandidateTransformation((), payload_prefixes=cast(tuple[str, ...], (1,))),
        ),
        (
            TypeError,
            "payload replacements must",
            lambda: CandidateTransformation(
                (), payload_replacements=cast(tuple[PayloadPrefixReplacement, ...], ("bad",))
            ),
        ),
        (
            ApplyProjectionError,
            "both write and delete",
            lambda: CandidateTransformation(
                (),
                payload_writes=(WorkspaceEntry.file("materialized/web/state", b"state"),),
                payload_deletes=("materialized/web/state",),
                payload_prefixes=("materialized/web",),
            ),
        ),
        (
            ApplyProjectionError,
            "cannot repeat a prefix",
            lambda: CandidateTransformation(
                (),
                payload_prefixes=("materialized/web",),
                payload_replacements=(replacement, replacement),
            ),
        ),
        (
            ApplyProjectionError,
            "cannot overlap",
            lambda: CandidateTransformation(
                (),
                payload_writes=(WorkspaceEntry.file("materialized/web/state", b"state"),),
                payload_prefixes=("materialized/web",),
                payload_replacements=(replacement,),
            ),
        ),
    )
    for error, message, construct in cases:
        with pytest.raises(error, match=message):
            construct()


def test_materialized_payload_replacement_is_exact_fenced_and_allows_pruning() -> None:
    initial = project_apply(
        _changes(_document("application")),
        current_desired=_desired(),
        observed=_observed(),
        context=_context(),
        validator=VALIDATOR,
    )
    current = _workspace(
        *initial.candidate.list_entries(),
        WorkspaceEntry.file("materialized/application/old.tf.json", b"old-state"),
    )

    class Compiler:
        def project(self, documents, current_desired, current_workspace, retained_sources, observed, context):  # type: ignore[no-untyped-def]
            expected_id, expected_entries = payload_prefix_evidence(current_workspace, "materialized/application")
            return CandidateTransformation(
                (current_desired[("unit.gitopsctr.io/v1", "Terraform", "application")],),
                payload_prefixes=("materialized/application",),
                payload_replacements=(
                    PayloadPrefixReplacement("materialized/application", expected_id, expected_entries),
                ),
            )

    result = project_apply(
        _changes(_document("application")),
        current_desired=_desired(current),
        observed=_observed(),
        context=_context(),
        validator=VALIDATOR,
        unit_compiler=Compiler(),
    )
    assert result.candidate.list_entries("materialized/application") == ()


def test_materialized_payload_replacement_rejects_stale_foreign_or_tampered_evidence() -> None:
    initial = project_apply(
        _changes(_document("application")),
        current_desired=_desired(),
        observed=_observed(),
        context=_context(),
        validator=VALIDATOR,
    )
    current = _workspace(
        *initial.candidate.list_entries(),
        WorkspaceEntry.file("materialized/application/state.json", b"current-state"),
    )

    class Compiler:
        def __init__(self, replacement: PayloadPrefixReplacement, prefix: str = "materialized/application") -> None:
            self.replacement = replacement
            self.prefix = prefix

        def project(self, documents, current_desired, current_workspace, retained_sources, observed, context):  # type: ignore[no-untyped-def]
            return CandidateTransformation(
                (current_desired[("unit.gitopsctr.io/v1", "Terraform", "application")],),
                payload_prefixes=(self.prefix,),
                payload_replacements=(self.replacement,),
            )

    expected_id, expected_entries = payload_prefix_evidence(current, "materialized/application")
    stale = PayloadPrefixReplacement("materialized/application", ContentId("sha256:" + "0" * 64), expected_entries)
    with pytest.raises(ApplyProjectionError, match="stale or corrupt"):
        project_apply(
            _changes(_document("application")),
            current_desired=_desired(current),
            observed=_observed(),
            context=_context(),
            validator=VALIDATOR,
            unit_compiler=Compiler(stale),
        )

    missing = PayloadPrefixReplacement("materialized/application", expected_id, expected_entries)
    with pytest.raises(ApplyProjectionError, match="stale or corrupt"):
        project_apply(
            _changes(_document("application")),
            current_desired=_desired(InMemoryWorkspace(initial.candidate.list_entries(), mutable=False)),
            observed=_observed(),
            context=_context(),
            validator=VALIDATOR,
            unit_compiler=Compiler(missing),
        )

    foreign_id, foreign_entries = payload_prefix_evidence(current, "materialized/other")
    foreign = PayloadPrefixReplacement("materialized/other", foreign_id, foreign_entries)
    with pytest.raises(ApplyProjectionError, match="emitted resource identities"):
        project_apply(
            _changes(_document("application")),
            current_desired=_desired(current),
            observed=_observed(),
            context=_context(),
            validator=VALIDATOR,
            unit_compiler=Compiler(foreign, "materialized/other"),
        )

    tampered = PayloadPrefixReplacement("materialized/application", expected_id, expected_entries)
    object.__setattr__(tampered, "prefix", "materialized/other")
    with pytest.raises(TypeError, match="modified after construction"):
        CandidateTransformation((), payload_prefixes=("materialized/application",), payload_replacements=(tampered,))


def test_retained_source_descriptor_rejects_post_issuance_tampering() -> None:
    source = _workspace(WorkspaceEntry.file("inputs/unit.yaml", b"source"))
    retained = _issue_retained_source(
        RetainedSourceHandle("retained-tamper"),
        RetentionStoreId("store-tamper"),
        SourceSnapshotId(SourceId("source-tamper"), SnapshotId("snapshot-tamper")),
        source.content_id,
    )
    descriptor = _source_plane(retained, source).descriptors[0]
    object.__setattr__(descriptor, "binding_key", "other")
    with pytest.raises(TypeError, match="modified after issuance"):
        descriptor._validate()


def test_named_source_context_allows_historical_binding_but_rejects_duplicate_or_ambiguous_snapshot() -> None:
    def named_source(snapshot: str, content: bytes, *, store: str) -> RetainedSourcePlane:
        workspace = _workspace(WorkspaceEntry.file("inputs/unit.yaml", content))
        retained = _issue_retained_source(
            RetainedSourceHandle(f"retained-{store}"),
            RetentionStoreId(f"store-{store}"),
            SourceSnapshotId(SourceId("workload"), SnapshotId(snapshot)),
            workspace.content_id,
        )
        plane = ExactPlane(
            HeadObservation.present(ChannelId(f"source/{store}"), SnapshotId(snapshot), f"{store}-incarnation"),
            workspace,
            SnapshotView(SnapshotId(snapshot), workspace.content_id, workspace),
        )
        descriptor = _issue_retained_source_descriptor(
            retained,
            "application-workload",
            SourceBindingRole.WORKLOAD,
            "inputs/unit.yaml",
            ContentId(f"selector-{store}"),
        )
        return RetainedSourcePlane(retained, plane, (descriptor,))

    historical = named_source("historical", b"historical", store="historical")
    current = named_source("current", b"current", store="current")
    context = ApplyProjectionContext(
        EnvironmentId("dev"),
        ChannelId("desired/dev"),
        ChannelId("observed/dev"),
        ChannelId("candidate/dev"),
        ApplyProjectionPolicy(),
        named_sources=(historical.descriptors[0], current.descriptors[0]),
        root_identity_issuer=ROOT_ISSUER,
    )
    context._validate()

    with pytest.raises(ApplyProjectionError, match="repeat exact"):
        ApplyProjectionContext(
            EnvironmentId("dev"),
            ChannelId("desired/dev"),
            ChannelId("observed/dev"),
            ChannelId("candidate/dev"),
            ApplyProjectionPolicy(),
            named_sources=(historical.descriptors[0], historical.descriptors[0]),
        )

    duplicate_snapshot = named_source("current", b"different", store="other-store")
    with pytest.raises(ApplyProjectionError, match="ambiguous retained source snapshot"):
        ApplyProjectionContext(
            EnvironmentId("dev"),
            ChannelId("desired/dev"),
            ChannelId("observed/dev"),
            ChannelId("candidate/dev"),
            ApplyProjectionPolicy(),
            named_sources=(current.descriptors[0], duplicate_snapshot.descriptors[0]),
        )

    object.__setattr__(current.descriptors[0], "binding_key", "tampered")
    with pytest.raises(TypeError, match="modified after issuance"):
        context._validate()


def test_project_apply_revalidates_all_retained_descriptor_binding_fields() -> None:
    source = _workspace(WorkspaceEntry.file("inputs/unit.yaml", b"source"))
    source_snapshot = SourceSnapshotId(SourceId("source-revalidation"), SnapshotId("snapshot-revalidation"))
    changes = AuthoredChangeSet(
        (_issue_authored_document("source", _document("application"), ContentId("sha256:" + "a" * 64)),),
        source_snapshot,
    )
    for field, value in (
        ("binding_key", "other"),
        ("role", SourceBindingRole.WORKLOAD),
        ("workspace_key", "other/unit.yaml"),
        ("selector_evidence", ContentId("sha256:" + "b" * 64)),
    ):
        retained = _issue_retained_source(
            RetainedSourceHandle(f"retained-{field}"),
            RetentionStoreId(f"store-{field}"),
            source_snapshot,
            source.content_id,
        )
        plane = _source_plane(retained, source)
        descriptor = plane.descriptors[0]
        context = ApplyProjectionContext(
            EnvironmentId("dev"),
            ChannelId("desired/dev"),
            ChannelId("observed/dev"),
            ChannelId("candidate/dev"),
            ApplyProjectionPolicy(),
            primary_source=descriptor,
            root_identity_issuer=ROOT_ISSUER,
        )
        object.__setattr__(descriptor, field, value)
        with pytest.raises(TypeError, match="modified after issuance"):
            project_apply(
                changes,
                current_desired=_desired(),
                observed=_observed(),
                retained_sources=(plane,),
                context=context,
                validator=VALIDATOR,
            )


def _artifact_request() -> tuple[ArtifactImportRequest, ResolvedArtifactImport]:
    source_desired = _plane("desired/staging", _workspace(), "artifact-staging-desired")
    source_observed = _plane("observed/staging", _workspace(), "artifact-staging-observed")
    promotion = _issue_promotion_source_descriptor(
        EnvironmentId("staging"),
        EnvironmentId("dev"),
        source_desired,
        source_observed,
        _desired(),
        _observed(),
        ContentId("sha256:" + "c" * 64),
    )
    lineage = PromotionLineage(
        EnvironmentId("staging"),
        "desired/staging",
        "a" * 40,
        "observed/staging",
        "b" * 40,
        "desired/dev",
        "c" * 40,
        "observed/dev",
        "d" * 40,
        ContentId("sha256:" + "c" * 64),
    )
    target = StackResource(
        GVK("gitopsctr.io/v1", "Stack"),
        ResourceMetadata(name="target", uid="d1-target"),
        StackSpec(template="template"),
    )
    imported = ArtifactImport(
        unit="unit",
        name="artifact",
        apiVersion="unit.gitopsctr.io/v1",
        kind="Terraform",
        fromPromotion=PromotionStackReference(stack="source"),
    )
    request = ArtifactImportRequest(imported, target, promotion, lineage)
    resolved = ResolvedArtifactImport(
        sourceStack="source",
        sourceStackUid="d1-source",
        sourceUnit="unit",
        sourceUnitUid="d1-unit",
        sourceDesiredRevision="a" * 40,
        sourceObservedRevision="b" * 40,
        receiptUnitContentId="sha256:" + "d" * 64,
        artifactName="artifact",
        apiVersion="unit.gitopsctr.io/v1",
        kind="Terraform",
        artifactDigest="sha256:" + "e" * 64,
        targetStackUid="d1-target",
        artifactDocument=JsonObjectValue({"version": 1}),
    )
    return request, resolved


def test_artifact_import_request_and_resolution_reject_tampered_evidence() -> None:
    request, resolved = _artifact_request()
    request._validate()
    resolution = ArtifactImportResolution(request, resolved)
    resolution._validate()

    for field, value in (
        (
            "target_stack",
            StackResource(
                GVK("gitopsctr.io/v1", "Stack"),
                ResourceMetadata(name="other", uid="d1-other"),
                StackSpec(template="template"),
            ),
        ),
        (
            "lineage",
            PromotionLineage(
                EnvironmentId("staging"),
                "other",
                "a" * 40,
                "observed/staging",
                "b" * 40,
                "desired/dev",
                "c" * 40,
                "observed/dev",
                "d" * 40,
                ContentId("sha256:" + "c" * 64),
            ),
        ),
    ):
        fresh, _unused = _artifact_request()
        object.__setattr__(fresh, field, value)
        with pytest.raises(TypeError, match="modified after construction"):
            fresh._validate()

    fresh, _unused = _artifact_request()
    other, _unused_other = _artifact_request()
    object.__setattr__(fresh, "promotion_source", other.promotion_source)
    with pytest.raises(TypeError, match="modified after construction"):
        fresh._validate()

    request, resolved = _artifact_request()
    resolution = ArtifactImportResolution(request, resolved)
    resolved.artifactDocument["version"] = 2
    with pytest.raises(TypeError, match="modified after construction"):
        resolution._validate()


def test_template_update_may_reproject_its_current_live_stack_root() -> None:
    template = _document("template", kind="StackTemplate")
    template["metadata"] = {"name": "template", "uid": "d1-template"}
    stack = _document("web", kind="Stack")
    stack["metadata"] = {"name": "web", "uid": "d1-web"}
    stack["spec"] = {"templateRef": {"name": "template"}}
    current = _workspace(
        WorkspaceEntry.file("stack-templates/template.json", json.dumps(template).encode()),
        WorkspaceEntry.file("stacks/web.json", json.dumps(stack).encode()),
    )

    class Compiler:
        def project(self, documents, current_desired, current_workspace, retained_sources, observed, context):  # type: ignore[no-untyped-def]
            return CandidateTransformation(
                (
                    ProjectedDocument("stack-templates/template.json", template),
                    ProjectedDocument("stacks/web.json", stack),
                )
            )

    result = project_apply(
        _changes(_document("template", kind="StackTemplate")),
        current_desired=_desired(current),
        observed=_observed(),
        context=_context(),
        validator=VALIDATOR,
        stack_compiler=Compiler(),
    )
    assert result.candidate.read("stacks/web.json")


def test_stack_delta_cannot_modify_or_delete_a_child_of_an_unrelated_stack() -> None:
    alpha = _document("alpha", kind="Stack")
    alpha["metadata"] = {"name": "alpha", "uid": "d1-alpha"}
    beta = _document("beta", kind="Stack")
    beta["metadata"] = {"name": "beta", "uid": "d1-beta"}
    beta_child = _document("db")
    beta_child["metadata"] = {
        "name": "db",
        "uid": "d1-beta-db",
        "ownerReferences": [{"apiVersion": beta["apiVersion"], "kind": "Stack", "name": "beta", "uid": "d1-beta"}],
    }
    current = _workspace(
        WorkspaceEntry.file("stacks/alpha.json", json.dumps(alpha).encode()),
        WorkspaceEntry.file("stacks/beta.json", json.dumps(beta).encode()),
        WorkspaceEntry.file("units/beta/db.json", json.dumps(beta_child).encode()),
    )

    class WriteOtherChild:
        def project(self, documents, current_desired, current_workspace, retained_sources, observed, context):  # type: ignore[no-untyped-def]
            return CandidateTransformation(
                (ProjectedDocument("stacks/alpha.json", alpha), ProjectedDocument("units/beta/db.json", beta_child))
            )

    class DeleteOtherChild:
        def project(self, documents, current_desired, current_workspace, retained_sources, observed, context):  # type: ignore[no-untyped-def]
            return CandidateTransformation(
                (ProjectedDocument("stacks/alpha.json", alpha),), deletes=("units/beta/db.json",)
            )

    for compiler, match in (
        (WriteOtherChild(), "child of an unrelated"),
        (DeleteOtherChild(), "child of an unrelated"),
    ):
        with pytest.raises(ApplyProjectionError, match=match):
            project_apply(
                _changes(_document("alpha", kind="Stack")),
                current_desired=_desired(current),
                observed=_observed(),
                context=_context(),
                validator=VALIDATOR,
                stack_compiler=compiler,
            )


def test_issued_promotion_lineage_binds_exact_planes_and_rejects_tampering() -> None:
    source_desired = _plane("desired/staging", _workspace(), "staging-desired")
    source_observed = _plane("observed/staging", _workspace(), "staging-observed")
    descriptor = _issue_promotion_source_descriptor(
        EnvironmentId("staging"),
        EnvironmentId("dev"),
        source_desired,
        source_observed,
        _desired(),
        _observed(),
        ContentId("sha256:" + "c" * 64),
    )
    context = WorkspaceProjectionContext(b'{"kind":"Project"}', b'{"kind":"Environment"}', promotion_source=descriptor)
    assert context.promotion_source is descriptor
    object.__setattr__(descriptor, "lineage_evidence", ContentId("sha256:" + "d" * 64))
    with pytest.raises(TypeError, match="modified after issuance"):
        context._validate()


def test_promotion_lineage_target_planes_must_match_apply_planes() -> None:
    source_desired = _plane("desired/staging", _workspace(), "staging-desired")
    source_observed = _plane("observed/staging", _workspace(), "staging-observed")
    historical_desired = _plane("desired/dev", _workspace(), "historical-desired")
    descriptor = _issue_promotion_source_descriptor(
        EnvironmentId("staging"),
        EnvironmentId("dev"),
        source_desired,
        source_observed,
        historical_desired,
        _observed(),
        ContentId("sha256:" + "e" * 64),
    )
    projection_context = WorkspaceProjectionContext(
        b'{"kind":"Project"}', b'{"kind":"Environment"}', promotion_source=descriptor
    )
    with pytest.raises(ApplyProjectionError, match="target desired plane"):
        project_apply(
            _changes(_document("application")),
            current_desired=_desired(),
            observed=_observed(),
            context=ApplyProjectionContext(
                EnvironmentId("dev"),
                ChannelId("desired/dev"),
                ChannelId("observed/dev"),
                ChannelId("candidate/dev"),
                ApplyProjectionPolicy(),
                projection_context=projection_context,
            ),
            validator=VALIDATOR,
        )


def test_source_backed_apply_requires_matching_retained_logical_content() -> None:
    source = _workspace(WorkspaceEntry.file("inputs/application.yaml", b"exact source bytes"))
    source_snapshot = SourceSnapshotId(SourceId("source"), SnapshotId("snapshot-1"))
    changes = AuthoredChangeSet(
        (_issue_authored_document("source", _document("application"), ContentId("sha256:" + "a" * 64)),),
        source_snapshot,
    )
    retained = _issue_retained_source(
        RetainedSourceHandle("retained-1"), RetentionStoreId("store-1"), source_snapshot, source.content_id
    )
    source_plane = _source_plane(retained, source)
    context = ApplyProjectionContext(
        EnvironmentId("dev"),
        ChannelId("desired/dev"),
        ChannelId("observed/dev"),
        None,
        ApplyProjectionPolicy(),
        primary_source=source_plane.descriptors[0],
        root_identity_issuer=ROOT_ISSUER,
    )

    result = project_apply(
        changes,
        current_desired=_desired(),
        observed=_observed(),
        retained_sources=(source_plane,),
        context=context,
        validator=VALIDATOR,
    )
    assert result.plan.primary_source is not None
    assert result.plan.primary_source.plane.content_id == source.content_id

    with pytest.raises(ApplyProjectionError, match="does not match retention evidence"):
        _source_plane(
            _issue_retained_source(
                RetainedSourceHandle("retained-2"),
                RetentionStoreId("store-1"),
                source_snapshot,
                ContentId("sha256:" + "b" * 64),
            ),
            source,
        )
