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

### 3. StackTemplate and Stack resolution — pending

### 4. Direct Stack operations and source pins — pending

### 5. Terraform/observed cleanup and Argo/forge integrations — pending

### 6. Acceptance harness and security/operations documentation — pending

### 7. Remove legacy compatibility after migration condition — pending
