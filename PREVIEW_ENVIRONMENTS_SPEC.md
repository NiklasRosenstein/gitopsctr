# Stacks and preview environments — living implementation spec

> **Status:** Living implementation design. The lifecycle-aware desired-resource envelope, hardened Unit-specific
> finalization slice, Stack/StackTemplate contracts, Stack projection, direct Stack instantiation, Stack deletion
> lifecycle, Stack-owned convergence ordering, and controller-owned source pins and claims are implemented, including
> terminal teardown evidence, explicit direct-Unit deletion, and direct Stack source-pin creation/release.
> Forge identity is provenance only. Forge-aware eligibility/recovery orchestration, merge enforcement, Argo manifest
> publication, and deployment-owned setup remain external or pending. Field names, document layouts,
> and command names are illustrative unless
> explicitly marked **Settled**.

Last updated: 2026-08-11

This document records the design decisions that should guide implementation. It complements the current
[concepts](docs/concepts.md), [resource model](docs/documents.md), and [receipt contract](docs/apis/receipt.md); those
documents describe supported behavior; the implementation status below identifies the parts of this work that have
already landed.

## Current implementation status

The current branch now has the Unit and core Stack lifecycle foundations, rather than an end-to-end preview-environment
implementation.

| Area | Status | Reconciliation |
| --- | --- | --- |
| Desired resource identity, lifecycle authority, ownership, legacy retention, rollback, and schema profiles | **Implemented for Unit, Stack, and StackTemplate** | The generic semantic model now covers the current resource families; future resource families may reuse it. |
| Unit deletion intent, owned-child obligations, UID/generation fencing, observed teardown evidence, effect leases, and Terraform destroy | **Implemented for Unit** | Commits `118429d`, `22d7814`, `816a8a4`, `26982bf`, `f9cc2ac`, and `8bb059e` close lease, incarnation, transition, dependency, legacy-safety, opaque-recovery, evidence-contract, direct-root, and post-evidence recovery defects; explicit operator resolution now covers permanently unparseable roots. Forge enforcement remains external. |
| Change-gated candidate freshness | **Local and CI verifier implemented** | `88ae0b9` rejects stale, rebased, multi-commit, merge, root, and missing-head candidates before review creation. CI now verifies the exact GitHub pull-request and merge-queue event head and target; required check and branch-protection policy remain forge configuration. |
| StackTemplate/Stack contracts and deterministic parameter expansion | **Implemented** | Commits `eb0bcb9` and `84d7ddb`; direct desired Stack provenance is typed and schema-published. |
| Generated Stack resource graphs with UID-fenced ownership | **Implemented projection and closure** | `b441941` and `d071c1e` project source-authored/direct Stack-owned Units and retain a UID-fenced closure through deletion. |
| Direct Stack instantiation | **Implemented with durable incarnation fencing** | `84d7ddb` adds replay-fenced `instantiate-stack` and exact template provenance; `a18c23f` and `f12329a` add desired-head and durable Stack tombstone fencing. A request ledger for richer replay history remains optional follow-up. |
| Direct and source-tracked Stack deletion/finalization | **Implemented core lifecycle** | `d071c1e` adds source-absence intents, direct UID/generation-fenced requests, child obligations, and root finalization after owned Units. |
| Controller-owned source pins | **Lifecycle and claims implemented** | `5d3a5a0` provides fenced refs and the current implementation adds CAS-fenced `gitopsctr/pin-claims/stacks/...` records. Direct Stack deletion and finalization retain UID-/revision-fenced pins and claims; orphan enumeration and reaping remain external CI responsibilities. |
| Forge provenance and recovery boundary | **Core boundary implemented; forge orchestration remains external** | Direct Stack provenance, source pins, claims, UID-fenced deletion, and finalization are core lifecycle primitives. PR/MR identity does not own desired resources. Forge CI may enumerate refs/resources, consult the forge, and invoke normal `gitopsctr` CLI primitives; the core has no forge eligibility or orphan-recovery command. |
| Stack dependency ordering | **Convergence, multi-instance isolation, and external-driver acceptance implemented** | `3b25f04` includes Stack-generated and desired-only Stack Units in convergence/status and preserves explicit cross-kind Stack edges during teardown; the current increment scopes generated names as `<stack>--<template-unit>`. The temporary-repository inventory harness and Docker/Terraform demo exercise dependency-safe external deployment and reverse teardown. |
| Argo integration and external publication | **Boundary and Argo absence observation implemented; external publication remains deployment-owned** | `46c2a67` documents the trusted ApplicationSet boundary, cleanup contract, and operations. Argo-backed Kubernetes Units now wait for Application absence during teardown, and the Kubernetes/Argo acceptance job proves external delivery and observation. This repository still does not publish preview manifests or own ApplicationSet resources. |
| End-to-end acceptance, security, operations, and legacy retirement | **Acceptance and operational guidance implemented; forge policy and retirement pending** | Operational/security guidance, direct and Stack-backed Docker/Terraform, Kubernetes, Argo, restart, focused recovery, Unit hardening acceptance, and a real temporary-repository Stack harness are implemented. The read-only compatibility audit covers one explicit ref or all Project-configured environments. Forge policy configuration and complete legacy migration remain open. |

The repository verification suite currently passes (`543` tests), including the landed concurrency, recovery,
incarnation, evidence, direct-root, and candidate-freshness regressions. Passing verification is therefore necessary,
not sufficient, for the remaining preview-environment milestones because external forge orchestration,
forge policy configuration, deployment-owned publication, and legacy migration are still open.
The current checkout has no `deployment/environments` path, so `audit-desired-compatibility --all` reports
`unavailable-environments-path`; a deployment-owned supported-ref inventory is still required before legacy retirement.

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
- Document the forge integration boundary and security model; keep forge policy and orchestration outside core
  lifecycle authority.

### Non-goals and deferred work

- **Deferred:** Packaging the source or Terraform module as an OCI bundle. The first implementation pins a
  controller-owned Git ref.
- **Deferred:** Automatically deleting empty Terraform state objects or backend keys after successful destroy.
- **Deferred:** Continuous tracking or promotion of one Stack from a changing StackTemplate.
- **Deferred:** Cross-ref ownership, multiple controlling owners, and a separate lifecycle database.
- **Non-goal:** Forge eligibility, expiry, and orphan detection inside `gitopsctr`. CI workflows or webhooks may
  consult the forge, enumerate preview lineage, and request operations through the normal CLI. A scheduled CI job
  recovers missed events; it is orchestration, not lifecycle authority.

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

### Forge provenance and orchestration boundary — Settled

- Forge identity such as provider, pull request or merge request number, source head, and forge URL is provenance.
  It MUST NOT be lifecycle authority, an owner reference, or a cascade-deletion trigger.
- PR CI or another trusted CI workflow MAY imperatively create, update, delete, and finalize preview Stacks through
  `gitopsctr` CLI primitives after applying its own eligibility and trust policy.
- `advance-desired`, reconciliation, and finalization MUST NOT require a live GitHub or GitLab API call.
- Missed forge events are handled by external scheduled CI. It enumerates preview refs or resources by lineage,
  consults the forge, and invokes ordinary `gitopsctr` operations for each cleanup. The core does not decide whether
  a PR or MR remains eligible.
- A repository-owned example workflow may document this pattern, but a forge-aware recovery command is not part of
  the core product contract.

### Finalization — Settled; Unit and Stack lifecycle implemented

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
5. A Stack finalization that removes a pinned root retains its deletion intent until the target transition is applied
   and the controller pin and claim are released under their UID-/revision fences. A later retry performs this cleanup;
   a pin or claim release failure MUST retain the intent and MUST NOT report cleanup complete.

With Git as the lifecycle authority, teardown therefore has at least a durable deleting transition followed by a
later absent transition. Tests MUST assert this ordering, not an exact commit count; progress may add commits.

Once deletion starts, the same UID cannot be revived. If external orchestration or source state requests the resource
again, a new lifecycle uses a new UID after the old lifecycle finalizes.

### Driver teardown — Settled for Unit; reused by Stack-owned Units

Teardown is an independent Unit-driver capability, parallel to planning, reconciliation, and verification. It
receives the retained desired Unit, its pinned source, a relevance-fenced prior receipt when one exists, the resource
UID, and the deletion generation. It MUST be idempotent. A matching terminal teardown-evidence record suppresses a
repeat driver call; terminal evidence is controller-owned and is not passed to a driver that will not be invoked.

A driver that may have external effects but cannot tear them down blocks finalization with an actionable status. A
successful Terraform destroy is sufficient confirmation that Terraform-managed resources are absent; gitopsctr does
not require deletion of the backend state object.

Successful `TeardownResult.details` are persisted in UID-/generation-fenced observed teardown evidence, with legacy
evidence documents interpreted as empty details. A crash before that evidence publication may retry the idempotent
driver operation. If evidence is already published but finalization publication fails, a fresh run reuses the matching
UID-/snapshot-fenced lease and evidence without invoking the driver again. The current contract does not invent a
separate in-progress evidence record. Receipt identity must
eventually distinguish resource incarnation and semantic desired generation. Whether this replaces the current
whole-document blob identity immediately or through a compatibility period is **Open**.

The current implementation covers source-tracked and explicitly direct Unit deletion intents, retained cleanup inputs,
owned-child obligations, UID-/generation-fenced teardown evidence, effect leases, and Terraform destroy. Direct Unit
roots retain their identity and cleanup inputs until explicit finalization; source absence never reclassifies a root as
direct or source-tracked. Core Stack finalization and UID-/revision-fenced source-pin retention and release are
implemented; forge-aware eligibility, enforcement, and acceptance work remain external or pending. The remaining Unit
correctness work is listed in
[Unit lifecycle hardening](#unit-lifecycle-hardening).

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
- **Done in `26982bf` and `8bb059e` — teardown evidence contract:** `TeardownContext` receives only a relevance-fenced prior receipt;
  terminal evidence is controller-owned and suppresses repeat teardown. `TeardownResult.details` are validated as
  strict JSON and persisted in UID-/generation-fenced observed evidence, with legacy evidence defaulting to empty
  details. A crash after evidence publication resumes finalization from the existing lease and does not repeat teardown.
- **Done in `f9cc2ac` — direct-Unit lifecycle:** `request-delete-direct-unit` creates an exact UID-/generation-fenced
  deletion intent only for a canonical directly managed root, retains the root even when authored source is absent,
  and reuses the existing finalization path. Source-tracked, owned, legacy, and stale-UID requests are rejected;
  repeated requests for the same direct intent are inert. Source material remains available to Terraform finalization
  when present. Forge-side enforcement preventing a stale change-gated candidate from merging after same-name
  recreation remains open.
- **Done in `88ae0b9` — candidate freshness verifier:** Change-gated publication now requires the candidate commit to
  have exactly one parent equal to the target head and exactly one commit after that parent. Roots, stale/rebased,
  multi-commit, merge, and missing-head candidates fail closed. The forge seam validates exact candidate/base heads
  for GitHub `pull_request` and `merge_group` payloads. Repository branch protection or a required check/merge queue
  is still needed to make this guarantee authoritative at merge time.
- **Acceptance coverage:** Add restart and failure-injection tests for every item above, including lease recovery,
  same-name recreation, GVK/driver replacement, resolved receipt/artifact dependencies, concurrent dependent
  insertion, legacy application, opaque-root recovery, and teardown evidence round trips.

## Stack resolution — Settled for this increment; core lifecycle implemented

- **Implemented in `eb0bcb9` and `b441941`:** A StackTemplate declares typed parameters and a template for API resources plus their
  dependency relationships. Parameter values are validated strictly, recursively expanded, and cannot contain
  receipt, artifact, or promotion references.
- **Implemented in `eb0bcb9` and `84d7ddb`:** Stack and StackTemplate authored/desired contracts and public schemas exist;
  a direct Stack records the exact StackTemplate revision/path/digest and request identity as provenance.
- **Implemented in `b441941` and `84d7ddb`:** A Stack contains concrete parameter values and resolves to concrete
  desired resources in one desired snapshot. Generated resources receive their own UIDs and a controlling owner
  reference to the Stack UID.
- **Implemented in `d071c1e`:** Source-tracked Stack disappearance creates a retained Stack deletion intent and
  Unit child intents during authoritative advance. Direct deletion creates the same UID-/generation-fenced Stack
  intent explicitly; Stack finalization removes the root only after every owned Unit and child intent is gone.
- **Implemented in `84d7ddb` and `d071c1e`:** A direct Stack retains a cleanup-capable snapshot and pins its exact
  template revision. The pin remains through teardown and is released only after successful finalization. It does not
  continuously follow later template changes in the first implementation.
- A source-tracked Stack is a concrete source resource. Whether it may also track or promote changes from a
  StackTemplate remains **Open**.
- StackTemplate dependency declarations are validated and retained in the projected graph. `3b25f04` integrates them
  into generic convergence/status and reverse teardown ordering. Generated Unit names are Stack-scoped in desired
  state, and intra-template receipt/artifact/promotion references are rewritten to the same concrete names. Restart
  and external-driver acceptance are covered by the temporary-repository inventory harness and Docker/Terraform demo.
- Secrets are references to an external secret mechanism, not plaintext Stack parameters committed to Git.

The implementation currently uses `deployment/environments/<environment>/stack-templates/` and `stacks/` for authored
resources, `stack-templates/` and `stacks/` in desired state, and the `instantiate-stack`,
`request-delete-direct-stack`, and `finalize-stack` commands. These names are **Settled for this increment**; future
API compatibility review may still revise them before production.

## Preview orchestration

### Complete infrastructure

Trusted pull-request CI instantiates a direct Stack from a trusted StackTemplate after applying its own eligibility
policy. Configuration supplies
the preview identity and values such as backend key, namespace, host name, and expiry. The Stack may own an OCI image
Unit followed by Terraform or other infrastructure Units.

Before forge refs can disappear, the workflow MUST pin the pull request head commit on a controller-owned Git ref.
Teardown uses that retained source and releases the pin only after finalization. OCI source bundles may replace this
later. Direct Stack creation and finalization wire a controller-owned pin and a CAS-fenced claim.
The workflow records forge identity as provenance and uses normal CLI operations for deletion and finalization.
External scheduled CI may compare claims, current desired state, finalized tombstones, published candidates, and forge
state before requesting cleanup. Pin reaping remains UID-/revision-fenced and follows normal finalization.

External CI treats a closed or merged pull request, removed eligibility label, or expiry as a cleanup condition. A
scheduled CI job compares direct Stack provenance with forge state to recover missed events. It requests cleanup through
normal Stack finalization, not an out-of-band deletion path.

### Existing Argo CD

The preferred first integration keeps one trusted, long-lived ApplicationSet using the pull-request generator while
gitopsctr publishes per-preview manifests to a controller-owned ref/path. Recommended ApplicationSet behavior:

- `applicationsSync: sync`
- `preserveResourcesOnDeletion: false`
- a managed `Namespace` manifest for a dedicated preview namespace; `CreateNamespace=true` alone is not sufficient
  cleanup ownership

The external Stack workflow and ApplicationSet MUST use the same eligibility gate. Closing a pull request makes it
ineligible; label removal does the same when a required preview label is configured. Expiry of an otherwise open pull
request must first remove that label or otherwise make the generator stop matching. If it cannot, finalization blocks
instead of deleting the manifests underneath a live Application.

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
- `tools/migrate_documents.py` converts known legacy desired refs to canonical YAML desired Units with deterministic
  source-tracked identity and follows the Project's configured desired and observed refs.
- Missing metadata MUST NOT be interpreted as direct management or an owner reference.
- A legacy root is adopted only while comparing it with an authoritative source snapshot. Adoption durably assigns
  explicit identity and management metadata; authoritative source absence begins normal finalization while retaining
  the legacy cleanup snapshot.
- Diagnostics and migration coverage remain until every supported desired ref has been adopted. The compatibility
  reader and this exception are then removed. The read-only
  `audit-desired-compatibility --environment ... --desired-ref ...` command emits
  versioned JSON and returns non-zero while one ref has legacy, partial,
  unparseable, ambiguous, opaque, or unverified cleanup state. The `--all` mode
  inventories Project-configured environments, resolves their desired refs,
  detects duplicate refs, and reports per-environment results. A complete
  supported-ref inventory and clean audit are still required before removal.

## Acceptance contract

The fast acceptance harness uses a real temporary Git repository and a deterministic external inventory
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

The harness covers the implemented Unit lifecycle path:

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
  can resolve it. **Covered by `816a8a4` and the UID-fenced `resolve-opaque-unit` operator path.**
- Pass a relevance-fenced prior receipt into teardown and prove driver result details survive restart after successful
  evidence publication while remaining UID/generation fenced. **Covered by `26982bf` and `8bb059e`, including a
  crash after evidence publication and before finalization publication.**
- Request deletion of a direct Unit with an exact UID, remove authored source if any, restart, and prove the direct Unit
  remains retained until finalization. **Covered by `f9cc2ac`; local candidate freshness is covered by `88ae0b9`, while
  forge-side required-check/merge-queue enforcement remains pending.**

Shared recovery coverage MUST prove that one destroy failure retains cleanup inputs across restart and that a stale
delete request for an old UID cannot affect a recreated same-name resource.

Commits `118429d`, `22d7814`, `816a8a4`, `26982bf`, `f9cc2ac`, `8bb059e`, and `88ae0b9` cover finalization lease completion, pre-effect
failure cleanup, same-name incarnation fencing, parseable GVK/driver replacement, resolved dependency preservation,
pre-lease dependency revalidation, legacy application blocking, parseable opaque-root recovery, the terminal teardown
evidence contract, explicit direct-Unit deletion, local candidate freshness fencing, and operator-confirmed resolution
of permanently unparseable roots. Forge-side required-check/merge-queue enforcement remains pending.

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
- [x] Reject stale or structurally unsafe change-gated candidates before creating a review request, and validate
  GitHub candidate/base head identity at the forge seam.

### Required to complete the Unit milestone

- [x] Close the implemented items in [Unit lifecycle hardening](#unit-lifecycle-hardening); lease, incarnation,
  parseable-transition, legacy-safety, parseable-opaque-recovery, terminal evidence, and direct-Unit lifecycle items
  are complete in `118429d`, `22d7814`, `816a8a4`, `26982bf`, and `f9cc2ac`.
- [x] Add controller-owned source-pin creation, retention, and UID-/revision-fenced release for direct Stacks.
- [x] Add CAS-fenced controller claim refs and retain direct Stack pins through gated or failed finalization; release
  pins and claims only after target finalization under UID-/revision fences. Unclaimed legacy pins remain retained for
  operator resolution; forge-aware orphan enumeration remains external.
- [x] Add the remaining Unit hardening acceptance scenarios and recovery cases, including restart after destroy
  failure, stale same-name deletion fencing, and operator resolution of an unparseable root; the local candidate
  freshness check is now also enforced by the GitHub CI job.

### Pending preview-environment feature work

- [x] Add generic StackTemplate and Stack contracts plus deterministic parameter expansion.
- [x] Add generated Stack resource graphs with UID-fenced controlling ownership.
- [x] Add direct Stack instantiation with request and revision fencing.
- [x] Add durable Stack incarnation tombstones; finalized Stack UIDs are carried through desired-state candidates,
  rollback, and source/direct recreation. A richer request ledger is optional follow-up rather than lifecycle authority.
- [x] Add direct Stack deletion requests with UID/generation fencing.
- [x] Generalize two-phase finalization and teardown ordering from Units to Stack-owned closures.
- [x] Integrate explicit StackTemplate dependencies, including cross-kind edges, into generic convergence/status and
  reverse teardown ordering; external-driver acceptance is covered by the inventory harness and Docker/Terraform demo.
- [x] Add focused Stack lifecycle, restart/recovery, and dependency-ordering acceptance coverage in
  `tests/test_preview_acceptance.py`; real Docker/Terraform, Kubernetes, and Argo acceptance jobs are present.
- [x] Add a real temporary-repository Stack harness with a deterministic external inventory, restart recovery, UID
  retention, reverse teardown, and same-name recreation assertions; Docker/Terraform driver-backed cleanup is covered
  by the demo acceptance flow.
- [x] Add instance-scoped generated Unit naming and acceptance coverage for two concurrent Stacks from one template.
- [x] Extend Docker/Terraform acceptance to add a source Stack, observe its generated Terraform Unit, remove the
  Stack, and finalize the Unit and Stack against the real Docker inventory.
- [x] Add Argo CD boundary examples, operations, security documentation, and external-delivery acceptance; native
  preview manifest publication and provider-specific setup remain deployment-owned.
- [x] Move forge eligibility, expiry, and orphan detection out of `gitopsctr`; retain forge provenance and lifecycle
  primitives, and keep cleanup UID-/revision-fenced. External CI owns eligibility and lineage enumeration.
- [x] Retire the forge-aware repository recovery workflow and command. Deployment-owned scheduled CI may enumerate
  preview lineage and invoke ordinary `gitopsctr` CLI operations.
- [x] Add a read-only GitHub branch-policy verifier for the required candidate-freshness check; live branch protection,
  ruleset, and merge-queue configuration remains deployment-owned.
- [x] Add a read-only compatibility audit for one desired ref and an aggregate mode for all Project-configured
  environments; keep legacy retirement pending until out-of-band refs are inventoried and every audit is clean.
- [ ] Configure and verify required forge freshness checks or merge-queue/branch-protection enforcement at merge time;
  repository CI now verifies GitHub `pull_request` and `merge_group` heads, but required-check policy remains external.
- [ ] Remove legacy implicit-root compatibility after the documented migration condition is met.
  The document migration tool now handles known source and configured desired refs; live retirement still requires an
  explicit supported-ref inventory and a clean compatibility audit.

## Open decisions

- Exact API fields, resource locations, CLI names, and Stack-local naming/collision rules.
- Stack and StackTemplate promotion or continuous-tracking semantics.
- Receipt generation/spec digest format and cleanup-evidence layout on the observed ref.
- Whether direct Argo CD Application management is supported alongside the preferred ApplicationSet integration.
- External CI garbage-collection boundary, lineage enumeration, and forge adapters.

## Decision log

| Date | Decision |
| --- | --- |
| 2026-08-10 | Keep Environment as the long-lived lane; model reusable graphs as StackTemplate and concrete lifecycles as Stack. |
| 2026-08-10 | Direct creation is a generic root management mode; an imperative request is provenance, not an owner. |
| 2026-08-10 | Desired-resource ownership is generic, desired-only, same-ref, and UID-fenced. |
| 2026-08-10 | Use a controller-owned Git source pin first; defer OCI source bundles. |
| 2026-08-10 | Cleanup is two-phase finalization; Terraform destroy success is sufficient and backend state files may remain. |
| 2026-08-10 | Pull request close, merge, label removal, and expiry make a preview ineligible; this policy is now applied by external CI, not by core lifecycle code. |
| 2026-08-10 | Scope generated Unit names to the concrete Stack as `<stack>--<template-unit>` (hash-bounded at 63 characters), and rewrite intra-template observation/artifact/promotion references to those names. |
| 2026-08-10 | Keep an explicit `management.mode` discriminator for root authority; empty marker objects add no needed semantics for the current contract. |
| 2026-08-10 | Unit lifecycle implementation is a foundation milestone, not completion of the end-to-end Stack/preview feature. Lease, incarnation, dependency, compatibility, opaque-root, evidence, and source-pin hardening remain tracked work. |
| 2026-08-11 | Use one CAS-fenced controller claim per Stack pin; recovery must verify the target and candidate revisions before reaping. Unclaimed legacy pins remain retained. |
| 2026-08-11 | Run repository CI for GitHub `merge_group` requests; required checks and merge-queue policy remain forge configuration. |
| 2026-08-11 | Validate Stack cleanup with the real Docker/Terraform drivers: add a source Stack, converge its generated Unit, remove it, and finalize the Unit before the Stack root. |
| 2026-08-11 | Resolve a permanently unparseable cleanup root only with an exact UID, explicit external-cleanup confirmation, and a durable Unit incarnation tombstone; parseable roots must use driver-backed recovery. |
| 2026-08-11 | Make document migration canonicalize legacy desired Units and resolve refs from the Project configuration; compatibility retirement still requires a complete supported-ref inventory and clean audit. |
| 2026-08-11 | Read-only GitHub API verification reports `main` is not branch protected; required freshness checks and merge-queue enforcement remain an external prerequisite. |
| 2026-08-11 | Treat forge identity as provenance only. PR CI may imperatively use `gitopsctr` lifecycle primitives; scheduled CI handles missed forge events by enumerating lineage and consulting the forge outside core lifecycle logic. |
