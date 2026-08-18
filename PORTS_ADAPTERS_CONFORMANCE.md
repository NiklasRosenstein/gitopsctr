# Ports and adapters conformance matrix

Status: phase-0 characterization record. This matrix distinguishes behavior
covered by the current Git implementation from behavior that still needs to be
made backend-independent. A passing Git test is not evidence that an in-memory
adapter conforms; the latter tests are intentionally listed as required gaps.

## Behavior matrix

| Capability / invariant | Current Git behavior and exact evidence | In-memory conformance test still required | Phase | Status |
| --- | --- | --- | --- | --- |
| Snapshot reads are immutable, addressable by an explicit revision, and missing-vs-named refs fail distinctly | Legacy evidence: `tests/test_apply.py::test_apply_with_source_revision_reads_snapshot_instead_of_dirty_worktree`; `tests/test_inventory.py::test_git_plane_session_caches_ref_resolution_materialization_and_blob_ids`; port evidence: `tests/test_snapshot_reader_conformance.py`; `tests/test_git_snapshot_reader.py` | Extend the mutable channel/store contract to select current, historical, candidate, and observed-absent state; prove stale observations are rejected by a consumer | 2–4 | Exact immutable snapshot reads and logical content identity conform for memory and Git; mutable channel semantics remain |
| Publication uses the exact expected head, including expected absence | `tests/test_state.py::test_first_owned_publication_fences_expected_absent_head_creation`; `tests/test_state.py::test_publication_cas_rejects_candidate_appearing_after_expected_absence`; `tests/test_state.py::test_publication_cas_rejects_target_sibling_before_push`; `tests/test_state.py::test_same_ref_target_interleave_uses_one_observation_and_fails_closed` | Race two publishers against an absent and present head; require stale observations to fail without changing ownership or accepted state | 4 | Git CAS coverage exists; no backend-neutral race suite |
| Publication is incarnation-fenced, including `A -> B -> A` (ABA) | Current tests fence target movement and candidate recreation: `tests/test_state.py::test_publication_recreation_fences_finalization_and_changes_marker`; `tests/test_state.py::test_candidate_recreation_race_fails_closed_during_owner_release`; `tests/test_state.py::test_absent_candidate_direct_recreation_race_fails_closed_without_lock_update` | Explicitly perform `A -> B -> A` on one channel and prove an old `HeadObservation(A)` cannot publish or clean up the new incarnation | 4 and 7 | **Gap:** no exact ABA conformance test was found |
| Proven lineage is required; inability to prove ancestry fails closed | `tests/test_apply.py::test_apply_carries_promotion_lineage_through_unrelated_and_noop_updates`; `tests/test_stack_template_acquisition.py::test_external_git_template_is_acquired_with_exact_lineage_and_clean_import_retention`; `tests/test_stack_contracts.py::test_promoted_artifact_lineage_requires_a_git_receipt_blob`; `tests/test_rollback.py::test_rollback_rechecks_target_ancestry_against_captured_current_head` | Model opaque lineage proofs independently of Git commit ancestry; test missing, conflicting, and stale proofs for apply, promotion, and rollback | 4, 6, and 7 | Git/domain behavior characterized; generic lineage port absent |
| Ambiguous publication retains enough intent and ownership for verification/retry | `tests/test_stack_source_pins.py::test_ambiguous_publication_releases_claim_only_after_owner_verification`; `tests/test_state.py::test_owner_survives_a_client_error_after_remote_publication`; `tests/test_stack_source_pins.py::test_source_materialization_recovers_from_exact_attempt_claim`; `tests/test_artifacts.py::test_observation_publication_retries_without_losing_concurrent_state` | Inject an outcome unknown to the caller after durable publication; retry using the retained `PublicationIntent`, verify exact ownership, and reject unsafe guesses | 4 and 7 | Git recovery behavior exists; no in-memory ambiguous-outcome suite |
| Retention and ownership protect required source/content and release only the exact owner | `tests/test_state.py::test_controller_pin_matching_release_removes_pin`; `tests/test_state.py::test_controller_pin_stale_release_fails_closed`; `tests/test_state.py::test_live_prepublication_claims_are_not_reaped`; `tests/test_stack_source_pins.py::test_failed_publication_does_not_release_a_preexisting_live_pin`; `tests/test_stack_source_pins.py::test_finalized_stack_releases_all_nested_aliases_for_exact_uid` | Exercise two owners, stale release, abandoned claim reaping, source disappearance, and exact-incarnation release with no Git refs or blob IDs in the contract | 4, 6, and 7 | Git retention tests exist; generic retention port missing |
| Effect authorization is exact-incarnation and lease-fenced | `tests/test_cli.py::test_effect_lease_is_cas_published_and_blocks_a_second_runner`; `tests/test_cli.py::test_effect_lease_precondition_rechecks_after_publish_race`; `tests/test_cli.py::test_effect_lease_heartbeat_renews_during_long_effect`; `tests/test_unit_hardening_acceptance.py::test_effect_lease_blocks_opaque_recovery`; `tests/test_finalization.py::test_reconcile_invalid_teardown_result_releases_lease` | Test typed reconcile vs teardown intents, lease loss, renewal, duplicate runner, stale resource UID, and recovery authorization using opaque tokens/generations | 5 and 7 | Git lease behavior is substantial; no port-level conformance suite |
| Rollback is a forward publication from retained historical content and is fenced | `tests/test_rollback.py::test_rollback_publishes_complete_forward_desired_state`; `tests/test_rollback.py::test_rollback_copies_exact_historical_payloads_and_removes_stale_files`; `tests/test_rollback.py::test_full_stack_rollback_rejects_recreated_root_identity`; `tests/test_rollback.py::test_historical_rollback_evidence_rejects_invalid_receipts`; `tests/test_rollback.py::test_rollback_dry_run_writes_no_deployment_ref` | Keep historical snapshots in an in-memory store; rollback after current-head movement, missing source, recreated identity, invalid evidence, and dry-run; prove no destructive rewind | 7 | Git rollback characterization exists; generic rollback orchestration missing |
| Review candidate acceptance requires exact proof and ownership adoption, not merge alone | `tests/test_forges.py::test_github_candidate_event_requires_exact_heads`; `tests/test_candidate_check.py::test_verify_event_accepts_exact_one_commit_candidate`; `tests/test_candidate_check.py::test_verify_event_rejects_stale_candidate`; `tests/test_state.py::test_merged_candidate_branch_releases_owner_and_canonical_source`; `tests/test_state.py::test_live_accepted_publication_owner_remains_protected` | Simulate review approval/merge independently from accepted-channel adoption; test missing adoption, stale candidate, target movement, and candidate recreation | 4 and 6 | Candidate proof/cleanup is covered; explicit backend-neutral adoption conformance is missing |
| Cleanup and recovery are ownership-aware, retry-safe, and fail closed on ambiguity | `tests/test_state.py::test_orphan_publication_owner_cleanup_removes_owner_canonical_and_lock`; `tests/test_state.py::test_stale_accepted_publication_owner_cleanup_preserves_advanced_ref`; `tests/test_state.py::test_candidate_absence_cleanup_fails_closed_on_target_movement`; `tests/test_automatic_deletion.py::test_same_name_recreation_cleanup_retries_against_tombstone_lease_store`; `tests/test_automatic_deletion.py::test_finalized_cleanup_fails_closed_on_ambiguous_driver_tombstones`; `tests/test_unit_hardening_acceptance.py::test_opaque_recovery_restores_parseable_payload_with_deletion_metadata` | Inject crashes at each cleanup step; retry with old/new incarnations, missing owner, active lease, ambiguous tombstone, and advanced accepted head; require idempotence and no deletion of live state | 5, 7, and 9 | Git cleanup/recovery evidence exists; no in-memory crash/retry suite |

## Current gaps and next acceptance targets

The current tests are strong characterization of the Git implementation, but
they do not yet prove the architecture's primary claim: the same application
guarantees hold without Git, refs, commits, filesystem paths, or a process
global state store. The highest-risk missing evidence is:

- shared in-memory conformance suites for mutable channels, publication,
  retention, lease, source, and accepted-candidate ports;
- an explicit `A -> B -> A` head-incarnation race;
- an ambiguous publication test that exercises durable `PublicationIntent`
  recovery through the application boundary;
- independent deployment-authority and accepted-snapshot tests;
- publication-level proof that stale snapshot/channel observations are rejected,
  including observed absence and exact historical/candidate selection;
- proof that all read-only commands and then apply/reconcile use an injected
  orchestrator rather than the global Git state store.

These gaps are phase 2–7 implementation work, not reasons to weaken the
current Git behavior. Until they are closed, phase 0 is documented but not
complete as an architecture-conformance claim.

## Acceptance gates applied to migration PRs

Each merged migration PR (#13, #14, and #15) was required to preserve the
repository acceptance gates from `AGENTS.md` and the contributor contract:

- the complete pytest suite, with coverage reporting;
- strict MkDocs build;
- Ruff lint and formatting checks;
- generated resource-model and schema freshness checks;
- `ty` type checking;
- focused characterization and import-boundary tests for the changed slice;
- `git diff --check` and a clean, intentionally scoped worktree before push.

Those gates establish regression safety for the merged baseline and resource
API extraction. They do **not** substitute for the in-memory conformance
suite listed above; every subsequent migration PR should add the relevant
in-memory cases while keeping the full gates green.
