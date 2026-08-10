# Stacks and preview environments — living implementation spec

> **Status:** Living implementation design. The lifecycle-aware desired-resource envelope and a hardened Unit-specific
> finalization slice are implemented, including terminal teardown evidence and explicit direct-Unit deletion. Direct
> Stack management, source pins, Stack, forge, and end-to-end acceptance remain pending. Field names, document layouts,
> and command names are illustrative unless
> explicitly marked **Settled**.

Last updated: 2026-08-10

This document records the design decisions that should guide implementation. It complements the current
[concepts](docs/concepts.md), [resource model](docs/documents.md), and [receipt contract](docs/apis/receipt.md); those
documents describe supported behavior; the implementation status below identifies the parts of this work that have
already landed.

## Current implementation status

The current branch is a Unit-focused foundation rather than an end-to-end preview-environment implementation.

| Area | Status | Reconciliation |
| --- | --- | --- |
| Desired Unit identity, lifecycle authority, ownership, legacy retention, rollback, and schema profiles | **Implemented for Unit** | Keep the generic semantic model; extend it to Stack and StackTemplate later. |
| Unit deletion intent, owned-child obligations, UID/generation fencing, observed teardown evidence, effect leases, and Terraform destroy | **Implemented for Unit** | Commits `118429d`, `22d7814`, `816a8a4`, `26982bf`, and `f9cc2ac` close lease, incarnation, transition, dependency, legacy-safety, opaque-recovery, evidence-contract, and direct-root lifecycle defects; source pins, forge enforcement, and acceptance work remains. |
| StackTemplate contracts, Stack contracts, parameter expansion, and generated ownership graphs | **Pending** | Milestone 3. |
| Direct Stack operations and UID-fenced deletion | **Pending** | Milestone 4. |
| Controller-owned source pins, forge eligibility/expiry/orphan recovery, and Argo integration | **Pending** | Milestones 4–5. |
| End-to-end acceptance, security, operations, and legacy retirement | **Pending** | Milestones 6–7. |

The repository verification suite currently passes (`456` tests), including the landed concurrency, recovery,
incarnation, evidence, and direct-root regressions. Passing verification is therefore necessary, not sufficient, for
the remaining preview-environment milestones because source pins, forge enforcement, Stack behavior, and end-to-end
acceptance contracts are still open.

## Problem and scope

Preview infrastructure needs a concrete lifecycle in desired Git state without requiring the preview instance to be
committed to the pull request branch. The same model should also support reusable, source-authored deployment stacks
outside preview workflows.

### Goals — Settled

- Support a source-authored `Stack` that resolves owned API resources, reconciles them, and cleans them up when the
  Stack disappears from its authoritative source snapshot.
- Support a source-authored `StackTemplate` that can be instantiated as a controller-authored `Stack` directly on a
  desired ref.
- Generalize lifecycle authority and desired-resource ownership beyond Stacks and Units.
- Resolve instance values such as Terraform backend keys, namespaces, and names in core from Stack parameters. Unit
  drivers consume resolved values; they do not allocate them.
- Make deletion durable, retryable, UID-fenced, and observable before desired documents disappear.
- Cover both complete preview infrastructure and deployment into an existing Argo CD installation.
- Document forge setup and the security boundary as part of the implemented feature.

### Non-goals and deferred work

- **Deferred:** Packaging the source or Terraform module as an OCI bundle. The first implementation pins a
  controller-owned Git ref.
- **Deferred:** Automatically deleting empty Terraform state objects or backend keys after successful destroy.
- **Deferred:** Continuous tracking or promotion of one Stack from a changing StackTemplate.
- **Deferred:** Cross-ref ownership, multiple controlling owners, and a separate lifecycle database.
- **Non-goal for the first implementation:** A built-in always-on forge watcher. Forge workflows or webhooks request
  operations; scheduled garbage collection recovers missed events.

## Resource roles

| Resource | Role |
| --- | --- |
| `Environment` | Long-lived policy, ref, change-gate, and promotion lane. It is not a preview instance. |
| `StackTemplate` | Inert, reusable parameter and resource-graph definition. It creates no Units or external effects by itself. |
| `Stack` | Concrete lifecycle owner with resolved parameters. It may be source-tracked or directly managed. |
| `Unit` | Driver-owned deployable resource. It may be a source-tracked root or a child owned by a Stack. |

A directly instantiated Stack has no source Stack document. Its management authority records that it is directly
managed; its template, request, forge event, promotion, and deployment source are provenance or inputs, not owners.

## Generic desired-resource lifecycle

### Authority and identity — Settled; Unit slice implemented

Every resource admitted to the desired lifecycle graph—initially Stack, StackTemplate, and Unit—MUST have an immutable
UID for one incarnation and exactly one lifecycle authority:

1. A root has desired-only management metadata with mode `sourceTracked` or `direct`.
2. A child has one controlling, UID-fenced owner reference to another desired resource.

The two forms are mutually exclusive. Initial ownership is restricted to the same desired ref, MUST be acyclic, and
MUST identify the owner by GVK, name, and UID. Recreating the same resource name after deletion creates a new UID.
Promotion, Receipt, artifact, and other lineage or evidence documents remain outside this graph unless a future API
explicitly gives them lifecycle-managed profiles.

Controller lifecycle metadata belongs in the desired resource envelope, not in a Unit driver's `spec`. A Unit's
`spec.source` continues to identify deployment code and is not lifecycle authority.

### Ownership, provenance, and dependencies — Settled

- Owner references define cascade boundaries.
- The Unit dependency DAG defines reconciliation order and reverse teardown order among owned resources.
- Template revisions, source revisions, promotions, request identities, and forge events are provenance. Provenance
  MUST NOT cause cascade deletion.
- A StackTemplate is not the owner of a Stack instantiated from it. The concrete Stack owns its generated resources.
- Stale operations MUST be fenced by resource UID and the expected desired revision or deletion generation.

The exact metadata field placement and spelling remain **Open**. The semantic distinctions above are settled.

The current Unit envelope uses an explicit `management.mode` discriminator with `sourceTracked` and `direct` values.
Keep this representation unless a later resource family needs materially different authority-specific fields; empty
`sourceTracked: {}` and `direct: {}` marker objects are not required for the current contract.

### Finalization — Settled; Unit slice implemented with hardening required

Deletion is monotonic for one UID and follows a durable finalization protocol:

1. A source-tracked root disappears from an authoritative source snapshot, or a direct root receives an explicit
   UID-fenced deletion request.
2. The controller commits deletion intent while retaining the root, its owned closure, source pins, and all teardown
   inputs.
3. Effectful children are torn down in reverse dependency order. Successful results are tied to the UID and deletion
   generation. Retries are safe after interruption.
4. Only after every teardown obligation succeeds may a later atomic desired transition remove the root and completed
   children. Active observations and materialized payloads are eventually removed from current state; Git history
   remains the audit trail.

With Git as the lifecycle authority, teardown therefore has at least a durable deleting transition followed by a
later absent transition. Tests MUST assert this ordering, not an exact commit count; progress may add commits.

Once deletion starts, the same UID cannot be revived. If eligibility or source state returns, a new lifecycle uses a
new UID after the old lifecycle finalizes.

### Driver teardown — Proposed; Unit capability implemented

Teardown is an independent Unit-driver capability, parallel to planning, reconciliation, and verification. It
receives the retained desired Unit, its pinned source, a relevance-fenced prior receipt when one exists, the resource
UID, and the deletion generation. It MUST be idempotent. A matching terminal teardown-evidence record suppresses a
repeat driver call; terminal evidence is controller-owned and is not passed to a driver that will not be invoked.

A driver that may have external effects but cannot tear them down blocks finalization with an actionable status. A
successful Terraform destroy is sufficient confirmation that Terraform-managed resources are absent; gitopsctr does
not require deletion of the backend state object.

Successful `TeardownResult.details` are persisted in UID-/generation-fenced observed teardown evidence, with legacy
evidence documents interpreted as empty details. A crash before that evidence publication may retry the idempotent
driver operation; the current contract does not invent a separate in-progress evidence record. Receipt identity must
eventually distinguish resource incarnation and semantic desired generation. Whether this replaces the current
whole-document blob identity immediately or through a compatibility period is **Open**.

The current implementation covers source-tracked and explicitly direct Unit deletion intents, retained cleanup inputs,
owned-child obligations, UID-/generation-fenced teardown evidence, effect leases, and Terraform destroy. Direct Unit
roots retain their identity and cleanup inputs until explicit finalization; source absence never reclassifies a root as
direct or source-tracked. Stack finalization and controller-owned source pins are not yet implemented. The remaining
correctness and acceptance work is listed in [Unit lifecycle hardening](#unit-lifecycle-hardening).

## Unit lifecycle hardening

The 2026-08-10 correctness review found the following work items in the implemented Unit slice. These are required
to close the Unit milestone; they are not changes to the settled lifecycle model.

- **Done in `118429d` — lease completion and recovery:** Finalization removes or completes its effect lease in the same UID- and
  revision-fenced publication as the Unit removal, or otherwise release it against the correct completion snapshot.
  Every known failure before an external effect begins (including retained-source materialization and driver lookup)
  MUST leave a retryable or explicitly recoverable state without an unintended permanent lease.
- **Done in `118429d` — incarnation fencing:** Persist monotonic incarnation state, a tombstone, or an equivalent durable nonce so that
  recreating a finalized name receives a new UID. Old teardown evidence MUST never satisfy a later incarnation, even
  when GVK, name, source, and source revision are identical.
- **Done in `118429d` — identity transitions:** A parseable GVK or driver transition MUST create a durable cleanup/finalization path for
  the old identity, or a durable migration record that makes the replacement safe. Repeated advance MUST not retain
  the old Unit forever without an intent or actionable operator path.
- **Done in `22d7814` — resolved dependency preservation:** Teardown ordering MUST retain receipt/artifact dependencies after resolution,
  either through `resolvedInputs`, deletion-intent metadata, or an equivalent normalized dependency graph. The
  producer MUST remain behind every dependent until the dependent's teardown is complete.
- **Done in `22d7814` — post-lease dependency revalidation:** Recheck the owned/dependent closure after acquiring the effect lease, or
  include the closure in the lease snapshot and reject changes to it. Candidate publication and external forge merges
  MUST not be able to introduce a new dependent into an already-running teardown without invalidating the operation.
- **Done in `816a8a4` — legacy reconciliation safety:** Applying a legacy desired Unit without an authoritative advance
  now stops before driver loading or effect-lease acquisition with migration guidance. `advance-desired` remains the
  adoption path and never infers direct management or ownership.
- **Done in `816a8a4` — opaque-root recovery:** `recover-opaque-unit` provides an explicit, UID-fenced, change-gated
  recovery path for parseable opaque roots, including source-absent roots and identity transitions. It preserves
  deletion intent and immutable cleanup fences, migrates generation-one intents, validates retained materialization,
  rejects stale source state, and never invokes reconciliation, teardown, or observed-evidence writes.
- **Done in `26982bf` — teardown evidence contract:** `TeardownContext` receives only a relevance-fenced prior receipt;
  terminal evidence is controller-owned and suppresses repeat teardown. `TeardownResult.details` are validated as
  strict JSON and persisted in UID-/generation-fenced observed evidence, with legacy evidence defaulting to empty
  details. A pre-publication crash remains a normal idempotent driver retry rather than an in-progress evidence state.
- **Done in `f9cc2ac` — direct-Unit lifecycle:** `request-delete-direct-unit` creates an exact UID-/generation-fenced
  deletion intent only for a canonical directly managed root, retains the root even when authored source is absent,
  and reuses the existing finalization path. Source-tracked, owned, legacy, and stale-UID requests are rejected;
  repeated requests for the same direct intent are inert. Source material remains available to Terraform finalization
  when present. Forge-side enforcement preventing a stale change-gated candidate from merging after same-name
  recreation remains open.
- **Acceptance coverage:** Add restart and failure-injection tests for every item above, including lease recovery,
  same-name recreation, GVK/driver replacement, resolved receipt/artifact dependencies, concurrent dependent
  insertion, legacy application, opaque-root recovery, and teardown evidence round trips.

## Stack resolution — Proposed

- A StackTemplate declares typed parameters and a template for API resources plus their dependency relationships.
- A Stack contains concrete parameter values and resolves to concrete desired resources in one desired snapshot.
- Generated resources receive their own UIDs and a controlling owner reference to the Stack UID.
- A direct Stack records the exact StackTemplate revision/path/digest as provenance and retains a cleanup-capable
  snapshot. It does not continuously follow later template changes in the first implementation.
- A source-tracked Stack is a concrete source resource. Whether it may also track or promote changes from a
  StackTemplate remains **Open**.
- Secrets are references to an external secret mechanism, not plaintext Stack parameters committed to Git.

Exact resource schemas, source paths, generated desired paths, name scoping, and CLI commands remain **Open**.

## Preview orchestration

### Complete infrastructure

An eligible pull request workflow instantiates a direct Stack from a trusted StackTemplate. Configuration supplies
the preview identity and values such as backend key, namespace, host name, and expiry. The Stack may own an OCI image
Unit followed by Terraform or other infrastructure Units.

Before forge refs can disappear, the workflow MUST pin the pull request head commit on a controller-owned Git ref.
Teardown uses that retained source and releases the pin only after finalization. OCI source bundles may replace this
later. Controller-owned pin creation and safe UID-/revision-fenced release are pending implementation; until then,
retaining only a source revision can leave finalization waiting after source garbage collection.

Closing a pull request, whether merged or unmerged, removing its eligibility label, or reaching its expiry requests
cleanup. A scheduled garbage collector compares direct Stack provenance with forge eligibility to recover missed
events. Cleanup proceeds through normal Stack finalization, not an out-of-band deletion path.

### Existing Argo CD

The preferred first integration keeps one trusted, long-lived ApplicationSet using the pull-request generator while
gitopsctr publishes per-preview manifests to a controller-owned ref/path. Recommended ApplicationSet behavior:

- `applicationsSync: sync`
- `preserveResourcesOnDeletion: false`
- a managed `Namespace` manifest for a dedicated preview namespace; `CreateNamespace=true` alone is not sufficient
  cleanup ownership

The Stack workflow and ApplicationSet MUST use the same eligibility gate. Closing a pull request makes it ineligible;
label removal does the same when a required preview label is configured. Expiry of an otherwise open pull request
must first remove that label or otherwise make the generator stop matching. If it cannot, finalization blocks instead
of deleting the manifests underneath a live Application.

Argo CD then removes the generated Application and cascades its managed resources. gitopsctr waits for the Application
and workloads to be absent before finalizing the Stack and removing its preview manifests. `PostDelete` hooks may
handle external effects that are not represented by Kubernetes resources.

Creating one temporary Argo CD Application directly instead of using an ApplicationSet remains an **Open** alternative
for deployments that cannot use the pull-request generator.

### Trust boundary — Settled

- The workflow, target revision, StackTemplate, and deployment policy are trusted control-plane inputs. Pull request
  source is a separate, potentially untrusted input.
- A `pull_request_target` workflow MUST NOT execute code or configuration checked out from the pull request.
- Pull-request-authored Terraform is arbitrary code and requires an explicit trust policy, isolation, and narrowly
  scoped credentials.
- Fork permissions, allowed templates/parameters, controller refs, and cleanup authority must be documented for each
  supported forge.

## Compatibility migration

Existing desired Units have no explicit UID or lifecycle authority. They are temporary legacy implicit
source-tracked roots:

- New desired-state writers MUST NOT emit the legacy shape.
- Missing metadata MUST NOT be interpreted as direct management or an owner reference.
- A legacy root is adopted only while comparing it with an authoritative source snapshot. Adoption durably assigns
  explicit identity and management metadata; authoritative source absence begins normal finalization while retaining
  the legacy cleanup snapshot.
- Diagnostics and migration coverage remain until every supported desired ref has been adopted. The compatibility
  reader and this exception are then removed.

## Acceptance contract

The planned fast acceptance harness will use a real temporary Git repository and a deterministic external inventory
driver that rejects dependency-unsafe deletion. A smaller integration against the existing Docker/Terraform demo will
prove that real Terraform destroy removes the managed container; `terraform state list` is empty although the state
file may remain. Out-of-band demo cleanup is only a final safety net.

### A. Source-tracked Stack deletion

- Project a committed source Stack and assert a source-tracked Stack UID plus correctly owned generated Units.
- Reconcile and observe external inventory and matching receipts.
- Remove the Stack in a new source commit and advance only through durable deletion intent.
- Assert the same UID, owned Units, and cleanup inputs remain while external resources still exist.
- Restart the controller harness, finalize in reverse dependency order, and observe empty external inventory.
- Assert teardown evidence is fenced to the UID/deletion generation, then only a later desired state removes the Stack
  and Units. Active Unit receipts and artifacts are removed or explicitly superseded; repeated convergence is inert.

### B. Direct Stack from a source-tracked StackTemplate

- Project a source-tracked StackTemplate and prove that it creates no Units or external effects by itself.
- Submit a direct instantiation with explicit parameters. Replaying the same request identity MUST yield one Stack
  lifecycle, not a duplicate.
- Assert direct management, pinned template provenance, Stack-owned Units, and an unchanged source ref.
- Reconcile and observe external inventory.
- Request UID-fenced deletion, checkpoint deletion intent, restart, and finalize in reverse dependency order.
- Assert fenced teardown evidence and removal or supersession of active Unit observations. The instance and its
  external inventory are absent while the StackTemplate retains the same UID and content.
- Any controller source pin remains through teardown and is released only after successful finalization.
- Repeated deletion is inert; recreating the name receives a new UID and cannot reuse old receipts.

### C. Unit lifecycle hardening

Before declaring the Unit implementation milestone complete, the harness MUST cover the already-landed Unit path:

- Complete finalization and prove the effect lease is removed or completed, including direct publication and
  pull-request/change-request publication paths.
- Inject source-materialization and driver-preparation failures before the external effect and prove retries do not
  inherit an unintended permanent lease.
- Finalize a Unit, recreate the same name with identical source identity, and prove the new UID cannot consume old
  teardown evidence.
- Change a Unit's GVK or driver and prove the old identity reaches durable finalization rather than livelocking.
- Resolve receipt and artifact references, then prove reverse teardown order remains safe after desired values replace
  the original expressions.
- Add a dependent concurrently between dependency inspection and lease acquisition, and prove teardown revalidates
  the closure or fences the operation.
- Apply a legacy desired Unit without `--advance` and prove it blocks safely with migration guidance. **Covered by
  `816a8a4`.**
- Create an opaque cleanup root, remove its source, restart, and prove the documented adoption or cleanup operation
  can resolve it. **Covered for parseable roots by `816a8a4`; permanently unparseable roots remain an explicit
  operator-resolution case.**
- Pass a relevance-fenced prior receipt into teardown and prove driver result details survive restart after successful
  evidence publication while remaining UID/generation fenced. **Covered for the terminal-evidence contract by
  `26982bf`; pre-publication crash retry remains an idempotence requirement.**
- Request deletion of a direct Unit with an exact UID, remove authored source if any, restart, and prove the direct Unit
  remains retained until finalization. **Covered by `f9cc2ac`; forge-side stale-candidate merge enforcement remains
  pending.**

Shared recovery coverage MUST prove that one destroy failure retains cleanup inputs across restart and that a stale
delete request for an old UID cannot affect a recreated same-name resource.

Commits `118429d`, `22d7814`, `816a8a4`, `26982bf`, and `f9cc2ac` cover finalization lease completion, pre-effect
failure cleanup, same-name incarnation fencing, parseable GVK/driver replacement, resolved dependency preservation,
pre-lease dependency revalidation, legacy application blocking, parseable opaque-root recovery, the terminal teardown
evidence contract, and explicit direct-Unit deletion. Source-pin, forge-side merge enforcement, permanently
unparseable-root operator resolution, and remaining acceptance scenarios remain pending.

## Implementation checklist

### Completed in the current Unit slice

- [x] Add lifecycle-aware desired Unit UID, root authority, UID-fenced ownership, and parsing/schema profiles.
- [x] Keep authored Unit documents name-only while emitting canonical desired envelopes.
- [x] Retain legacy Units as source-tracked compatibility roots during authoritative comparison.
- [x] Add Unit source-absence deletion intents, retained cleanup inputs, owned-child obligations, and rollback
  preservation.
- [x] Add Unit driver teardown and Terraform destroy capability.
- [x] Add UID/generation-fenced observed teardown evidence and retry-safe effect-lease publication.
- [x] Add Unit reconciliation/status diagnostics and generated schema coverage.
- [x] Block legacy reconciliation without authoritative adoption and add UID-/generation-fenced recovery for parseable
  opaque cleanup roots, including source-absent and identity-transition cases.
- [x] Pass relevant prior receipts to teardown and persist strict-JSON driver details in UID-/generation-fenced terminal
  observed evidence, including legacy evidence compatibility.
- [x] Add explicit UID-fenced deletion requests and finalization for direct Unit roots without inferring direct
  management from source absence.

### Required to complete the Unit milestone

- [ ] Close the remaining items in [Unit lifecycle hardening](#unit-lifecycle-hardening); lease, incarnation,
  parseable-transition, legacy-safety, parseable-opaque-recovery, terminal evidence, and direct-Unit lifecycle items
  are complete in `118429d`, `22d7814`, `816a8a4`, `26982bf`, and `f9cc2ac`.
- [ ] Add controller-owned source-pin creation, retention, and UID-/revision-fenced release.
- [ ] Add the Unit hardening acceptance scenarios and recovery cases.

### Pending preview-environment feature work

- [ ] Add generic StackTemplate and Stack contracts plus deterministic parameter expansion.
- [ ] Add generated Stack resource graphs with UID-fenced controlling ownership.
- [ ] Add direct desired-resource operations with request and revision fencing.
- [ ] Generalize two-phase finalization and teardown ordering from Units to Stack-owned closures.
- [ ] Add the two Stack acceptance scenarios and restart/recovery cases.
- [ ] Extend Docker/Terraform acceptance to observe Stack-driven cleanup.
- [ ] Add GitHub/GitLab setup, Argo CD examples, operations, and security documentation.
- [ ] Add scheduled orphan detection, forge eligibility, and expiry cleanup.
- [ ] Remove legacy implicit-root compatibility after the documented migration condition is met.

## Open decisions

- Exact API fields, resource locations, CLI names, and Stack-local naming/collision rules.
- Stack and StackTemplate promotion or continuous-tracking semantics.
- Receipt generation/spec digest format and cleanup-evidence layout on the observed ref.
- Whether direct Argo CD Application management is supported alongside the preferred ApplicationSet integration.
- Exact garbage-collection command/service boundary and forge adapters.

## Decision log

| Date | Decision |
| --- | --- |
| 2026-08-10 | Keep Environment as the long-lived lane; model reusable graphs as StackTemplate and concrete lifecycles as Stack. |
| 2026-08-10 | Direct creation is a generic root management mode; an imperative request is provenance, not an owner. |
| 2026-08-10 | Desired-resource ownership is generic, desired-only, same-ref, and UID-fenced. |
| 2026-08-10 | Use a controller-owned Git source pin first; defer OCI source bundles. |
| 2026-08-10 | Cleanup is two-phase finalization; Terraform destroy success is sufficient and backend state files may remain. |
| 2026-08-10 | Pull request close, merge, label removal, and expiry all make a preview ineligible and request cleanup. |
| 2026-08-10 | Keep an explicit `management.mode` discriminator for root authority; empty marker objects add no needed semantics for the current contract. |
| 2026-08-10 | Unit lifecycle implementation is a foundation milestone, not completion of the end-to-end Stack/preview feature. Lease, incarnation, dependency, compatibility, opaque-root, evidence, and source-pin hardening remain tracked work. |
