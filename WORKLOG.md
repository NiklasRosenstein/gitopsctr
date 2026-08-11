# Preview environments implementation work log

## Run configuration

- Work source: `PREVIEW_ENVIRONMENTS_SPEC.md` (named by the user; no separate roadmap exists).
- Operating mode: Mode B intended; work is isolated on the `codex/preview-environments` feature branch.
- Dispatch: native parallel sub-agents; implementation uses Luna High with high reasoning; advisory/review uses Sol High only.
- Status medium: this file plus concise milestone recaps in the task.
- Deployment: CI-driven only if configured; no manual deployment.
- Escalation: in conversation.
- Review dial: high for lifecycle and schema changes.

## Initial state

- User changes present before implementation: modified `AGENTS.md` and untracked `PREVIEW_ENVIRONMENTS_SPEC.md`; do not overwrite or stage them accidentally.
- No remote push, merge, or deployment performed.

## Milestones

### 1. Migration-safe desired-resource foundation — complete

Acceptance criteria:

- Desired resources can carry immutable per-incarnation identity and exactly one root authority or UID-fenced owner.
- Authored Unit metadata remains name-only.
- Legacy desired Units remain source-tracked compatibility roots and are never inferred as direct or owned.
- New desired writers emit canonical metadata and preserve existing identities across retries.
- Source-absent legacy/current Units are retained with cleanup inputs until finalization exists.
- Focused round-trip, schema, adoption, identity-retention, collision, and source-absence tests pass.

Implementation owner: Luna High. Advisory: Sol High.

Post-pull status: upstream commit `07c722a` added operation-specific source-revision policy. Its overlap with the desired-state preparation loop was resolved in `src/gitopsctr/cli.py`, preserving both behaviors. The focused affected tests passed (`124 passed`), and the full post-pull check passed (`367 passed`, lint, typecheck, schema freshness, strict docs, and actionlint).

Completed this milestone with canonical desired metadata, deterministic source-tracked UIDs, transition blocking, opaque cleanup envelopes for unavailable drivers, durable repeated-advance retention, rollback identity/lifecycle preservation, strict cleanup-envelope validation, and reconciliation/status diagnostics. Sol High's final advisory pass found no remaining blockers. Final verification: `UV_CACHE_DIR=/tmp/gitopsctr-uv-cache mise run check` passed with 389 tests, lint, typecheck, schema freshness, strict docs, and actionlint.

### 2. Durable finalization and teardown — complete for the implemented Unit slice

Implemented source-absent Unit deletion intents, UID/generation fencing, optional driver teardown, Terraform destroy,
owned-child obligations, observed teardown evidence, CAS effect leases with explicit recovery, rollback preservation,
opaque cleanup blocking, and retry-safe finalization. Implementation owner: Luna High; advisory: Sol High.

Final verification: `GIT_CONFIG_GLOBAL=/dev/null UV_CACHE_DIR=/tmp/gitopsctr-uv-cache mise run check` passed with
417 tests, lint, typecheck, schema freshness, strict docs, and actionlint. Sol High's final advisory found no
remaining blockers for this implemented slice. Stack/StackTemplate, direct Stack operations, forge/source-pin
orchestration, and full acceptance scenarios remain future milestones.

### 2.1 Unit lifecycle hardening — complete for this increment

Commit `118429d` closed the reviewed Unit defects for effect-lease completion and pre-effect failure cleanup,
incarnation/tombstone migration and rollback fencing, and parseable GVK/driver transition finalization. It also made
pull-request finalization reporting truthful when only a candidate advances and added focused regression coverage.

Verification: `GIT_CONFIG_GLOBAL=/dev/null UV_CACHE_DIR=/tmp/gitopsctr-uv-cache mise run check` passed with 424 tests,
lint, typecheck, schema freshness, strict docs, actionlint, and formatting. Sol High final review returned no
confirmed or plausible findings. Remaining Unit backlog: resolved dependency preservation and post-lease
revalidation, legacy/opaque-root operator recovery, teardown evidence plumbing, direct-root deletion, source pins,
and the remaining acceptance scenarios.

### 2.2 Unit dependency and lease-CAS hardening — complete for this increment

Commit `22d7814` preserves resolved receipt/artifact producer edges in deletion intents and teardown ordering,
explicitly excludes promotion provenance, and validates malformed dependency keys fail closed. It also evaluates the
teardown-dependent precondition inside every effect-lease CAS attempt and refreshes the local desired tree when the
lease acquisition advances the revision.

Verification: `GIT_CONFIG_GLOBAL=/dev/null UV_CACHE_DIR=/tmp/gitopsctr-uv-cache mise run check` passed with 427 tests,
lint, typecheck, schema freshness, strict docs, actionlint, and formatting. Sol High review returned no confirmed or
plausible findings. Remaining Unit backlog: legacy/opaque-root operator recovery, teardown evidence plumbing,
direct-root deletion, controller-owned source pins, forge-side merge enforcement, and the remaining acceptance cases.

### 2.3 Unit compatibility recovery — complete for this increment

Commit `816a8a4` closes the legacy/opaque recovery gap. Reconciliation now blocks legacy desired Units before driver or
effect execution until `advance-desired` adopts them against an authoritative source revision. The new
`recover-opaque-unit` operation restores parseable opaque cleanup roots under an exact UID fence, preserves immutable
deletion-intent identity and dependencies, migrates generation-one intents, validates/copies materialization payloads,
handles source absence and identity transitions, and rejects stale same-identity source state. It is publication-gated
and does not reconcile, tear down, or write observed evidence.

Verification: `GIT_CONFIG_GLOBAL=/dev/null UV_CACHE_DIR=/tmp/gitopsctr-uv-cache mise run check` passed with 437 tests,
lint, typecheck, schema freshness, strict docs, actionlint, and formatting. Sol High found six confirmed defects in the
initial recovery patch; Luna High addressed all six and the regression suite passed. Remaining Unit backlog:
teardown-evidence plumbing, direct-root deletion, controller-owned source pins, permanently unparseable-root operator
resolution, forge-side merge enforcement, and the remaining acceptance cases.

### 2.4 Unit teardown-evidence contract — complete for this increment

Commit `26982bf` passes a relevance-fenced prior receipt into Unit teardown and persists strict-JSON
`TeardownResult.details` in UID-/deletion-generation-fenced observed teardown evidence. Matching terminal evidence
suppresses repeat teardown, legacy evidence documents remain readable with empty details, and malformed/non-finite
driver details are rejected. The contract intentionally treats a crash before evidence publication as an idempotent
driver retry; it does not add a separate in-progress evidence record.

Verification: `GIT_CONFIG_GLOBAL=/dev/null UV_CACHE_DIR=/tmp/gitopsctr-uv-cache mise run check` passed with 446 tests,
lint, typecheck, schema freshness, strict docs, actionlint, and formatting. Sol High's final review confirmed the
prior-receipt relevance and terminal-evidence sequencing; its only additional finding was the non-finite JSON case,
which Luna High fixed centrally with regression coverage. Remaining Unit backlog: direct-root deletion,
controller-owned source pins, permanently unparseable-root operator resolution, forge-side merge enforcement, and the
remaining acceptance cases.

### 2.5 Direct Unit deletion lifecycle — complete for this increment

Commit `f9cc2ac` adds `request-delete-direct-unit` with exact UID fencing, canonical direct-management validation,
durable deletion intent, retained direct roots when authored source is absent, change-gated candidate publication, and
reuse of the existing finalization path. Repeated requests for the same direct UID and intent are inert. Directness
controls deletion authority; retained source is still materialized for Terraform finalization when present. Source-
tracked, owned, legacy, and stale-UID requests are rejected.

Verification: `GIT_CONFIG_GLOBAL=/dev/null UV_CACHE_DIR=/tmp/gitopsctr-uv-cache mise run check` passed with 456 tests,
lint, typecheck, schema freshness, strict docs, actionlint, and formatting. Sol High's review found and Luna High
fixed the direct finalization source-materialization and repeated-request defects. The remaining gaps are
controller-owned source pins, permanently unparseable-root operator resolution, forge-side stale-candidate merge
enforcement, and the remaining acceptance cases.

### 2.6 Change-gated candidate freshness — local hardening complete

Commit `88ae0b9` adds a fail-closed Git commit-graph verifier for every locally published change candidate. A candidate
must be exactly one controller commit whose sole parent is the target desired head; stale, rebased, multi-commit,
merge, root, and missing-head candidates are rejected before a review request is opened. The forge seam also validates
exact candidate/base heads for GitHub `pull_request` and `merge_group` event payloads.

Verification: `GIT_CONFIG_GLOBAL=/dev/null UV_CACHE_DIR=/tmp/gitopsctr-uv-cache mise run check` passed with 468 tests,
lint, typecheck, schema freshness, strict docs, actionlint, and formatting. Sol High confirmed the original stale-merge
race and advised this narrow verifier. Repository branch-protection or required-check/merge-queue configuration is
still needed for an authoritative merge-time guarantee; source pins, permanently unparseable-root operator
resolution, and the remaining acceptance cases are also pending.

### 3. StackTemplate and Stack resolution — core lifecycle and acceptance complete

Commits `eb0bcb9`, `b441941`, and `84d7ddb` add typed authored/desired StackTemplate and Stack contracts, public schema publication, strict
parameter declarations for string/integer/number/boolean/object/array values, recursive `fromParameter` expansion,
and deterministic dependency-graph validation. StackTemplates are represented as inert reusable definitions; source
and direct Stack projection now emits UID-fenced owned Units, and direct instantiation persists exact template
provenance with replay/collision fencing. Commit `d071c1e` adds source-absence Stack intents, direct deletion requests,
owned-Unit closure fencing, and Stack finalization after child teardown. Commit `3b25f04` integrates StackTemplate
dependencies into generic convergence/status and reverse teardown ordering; the follow-up correctness pass also
preserves cross-kind edges. Generated Unit names are now Stack-scoped, allowing concurrent instances from one
template; temporary-repository and driver-backed acceptance coverage is now present.

Verification: `GIT_CONFIG_GLOBAL=/dev/null UV_CACHE_DIR=/tmp/gitopsctr-uv-cache mise run check` passed with 548 tests,
lint, typecheck, schema freshness, strict docs, actionlint, formatting, and diff checks. No required Stack resolution
work remains in this milestone.

### 4. Controller-owned source-pin lifecycle — lifecycle and claim recovery complete

Commit `5d3a5a0` adds idempotent controller-owned Git pin creation and UID/revision-fenced release without force-push,
with bare-repository coverage for create, repeat, mismatched create, matching release, stale release, and missing
release. Commits `84d7ddb` and `d071c1e` wire pin creation into direct Stack instantiation and release into successful
Stack finalization, retaining the pin through change-gated teardown. Commit `36a529b` adds validated pin enumeration,
fail-closed GitHub eligibility/expiry evaluation, and `recover-orphaned-stacks`, which routes ineligible direct Stacks
through the normal UID-fenced deletion lifecycle. Commit `57dd132` releases pins confirmed by finalized Stack
tombstones and cleans up pins when publication is proven not to have reached a desired/candidate ref. The current
increment adds CAS-fenced `gitopsctr/pin-claims/stacks/...` records, candidate ownership checks, reaping, and claim
cleanup. Unclaimed legacy pins remain retained. Native GitLab.com lookup is provided by the read-only `glab` adapter.
Self-hosted GitLab setup, scheduler wiring, and authoritative merge-time forge enforcement remain external follow-up
work.

### 5. Direct Stack lifecycle — core and acceptance complete

Commit `84d7ddb` adds `instantiate-stack`, exact template revision/path/digest provenance, request replay fencing,
direct management metadata, and Stack-owned generated Units. The focused CLI/contract/graph tests and full 548-test
repository check pass. Commit `d071c1e` adds `request-delete-direct-stack`, source-absence handling for source-tracked
Stacks, child deletion obligations, UID/generation fencing, and `finalize-stack`; the focused Stack suite and full
548-test repository check pass. Restart acceptance and external-driver cleanup coverage are now present; dependency
ordering is now implemented and covered by focused graph tests. Commit `1a4bf66` adds focused acceptance coverage
for source-tracked cleanup across a desired-state restart, direct finalization retry after publication failure, and
public dependency ordering. Commit `a18c23f` adds a desired-head incarnation fence and exact owner checks. Commit
`f12329a` adds durable `.gitopsctr/incarnations/stacks` tombstones, carries them through desired candidates and
rollback, and fences source/direct recreation from finalized UIDs. A richer request ledger remains optional follow-up
work.

### 6. Forge recovery and operational boundary — core recovery complete; external integrations pending

Commit `36a529b` adds GitHub pull-request identity parsing, fail-closed eligibility checks for closed/merged or
label-ineligible previews, optional expiry handling, controller-pin enumeration, and present-root recovery through
normal Stack deletion intents. The correctness pass accepts canonical GitHub identities, enforces exact Stack owner
UIDs, preserves cross-kind dependency edges, and makes recovery dry-run inspect pins. Commit `46c2a67` documents the
ApplicationSet, security, and cleanup boundary for Argo CD. Commit `57dd132` adds finalized-tombstone and
pre-publication orphan-pin cleanup safeguards. The current increment adds CAS-fenced pin claims and reaping. The
read-only GitLab.com eligibility adapter uses `glab`; the repository still does not publish preview manifests or own
ApplicationSet resources, and forge-side required checks/merge queues remain deployment configuration. CI now runs for
GitHub `merge_group` requests and verifies the exact candidate/base commit shape.

Verification: `GIT_CONFIG_GLOBAL=/dev/null UV_CACHE_DIR=/tmp/gitopsctr-uv-cache mise run check` passed with 548 tests,
lint, typecheck, schema freshness, strict docs, actionlint, formatting, and diff checks.

### 7. Acceptance harness and security/operations documentation — acceptance and documentation complete

The operational/security guide is published at `docs/preview-environments.md` and linked from the MkDocs navigation.
Docker/Terraform, Kubernetes, and Argo acceptance jobs are present. The Docker acceptance now adds a source Stack,
observes its generated Terraform Unit, removes the Stack, and finalizes the real container through the normal
UID-fenced Unit and Stack lifecycle. Argo-backed Kubernetes teardown waits for Application absence. The temporary-
repository Stack harness proves restart recovery, UID retention, reverse teardown, and same-name recreation against a
deterministic inventory. Remaining work is required forge policy configuration and legacy migration completion.

### 7.1 Unit acceptance and opaque-root resolution — complete

The new Unit acceptance coverage proves a transient destroy failure remains retryable after a fresh finalization
attempt, and that a stale deletion request cannot target a recreated same-name Unit. `resolve-opaque-unit` now gives
operators a UID-fenced path for permanently unparseable cleanup roots. It requires explicit external-cleanup
confirmation, rejects parseable roots, active leases, and deletion intents, and writes a finalized Unit incarnation
tombstone before publishing the resolution.

Commit `8bb059e` closes the remaining pre-publication recovery gap. If teardown evidence is published but finalization
publication fails, a fresh controller run reuses the matching durable effect lease and evidence, removes the deletion
intent, and does not invoke the external driver a second time. Focused acceptance coverage now has five tests. The
full repository verification passes with 550 tests, lint, typecheck, schema freshness, strict docs, actionlint,
formatting, and diff checks.

### 8. Remove legacy compatibility after migration condition — pending

Legacy desired-resource compatibility remains intentionally enabled. It can be removed only after every supported
desired ref has explicit lifecycle metadata and the migration/adoption diagnostics are no longer needed.

### 8.1 Migration tooling hardening — complete for known refs

The document migration tool now assigns deterministic source-tracked metadata to legacy desired Units, updates their
source revision to the migrated source commit, preserves existing Project ref configuration, and migrates configured
desired and observed refs instead of assuming `deploy/*` and `observed/*`. Live compatibility retirement remains open
until deployments provide a complete supported-ref inventory and pass a clean audit.

### 8.2 Desired compatibility audit — complete for one explicit ref

The read-only `audit-desired-compatibility` command now materializes one exact desired ref and emits versioned JSON.
It reports legacy or partial Units, invalid resource graphs, ambiguous cleanup state, unverified deletion identities,
and opaque cleanup roots. It returns non-zero when findings exist. Three focused tests cover clean canonical state,
legacy state, and unsafe cleanup state. Full verification remains at 553 tests. Live legacy retirement still needs a
complete supported-ref inventory and a clean audit for every ref.
