# Ports and adapters migration baseline

Status: phase 0 inventory captured alongside `PORTS_ADAPTERS.md`.

The executable-behavior evidence and remaining backend-neutral acceptance gaps
are tracked in `PORTS_ADAPTERS_CONFORMANCE.md`.

This document records where the current implementation crosses the target
architecture boundaries. It is an inventory, not a promise to preserve the
current module layout or interfaces. Each category names its intended migration
phase so later changes can be checked against an explicit baseline.

## Current ownership

| Current module | Responsibilities currently combined | Intended destination | Phase |
| --- | --- | --- | --- |
| `api.py` | GVK/API-kind values, entry-point discovery, process cache, authoritative registration checks | values in `resource_api`; discovery in the default composition | 1 |
| `document.py` | JSON/document primitives, Mashumaro helpers, authored-reference rejection | primitives in `resource_api`; GitOpsCtr helpers in the domain/catalog layer | 1 |
| `resource_model.py` | identities, addressing, topology, planes/scopes, collections, filesystem discovery, presenters, domain bindings, registry construction | kernel registry plus GitOpsCtr catalog and collection adapters | 1 and 3 |
| `registry.py` | installed contribution discovery, global catalog construction, driver dispatch helpers | default composition and GitOpsCtr catalog | 1, 2, and 9 |
| `inventory.py` | snapshot materialization, collection discovery, graph evaluation, inspection preparation | application inspection service plus logical collection adapter | 3 |
| `plane_repositories.py` | Git-backed ref reads, temporary checkouts, blob IDs | snapshot-store and workspace adapters | 3 |
| `controller.py` | CLI, composition, parsing, transitions, Git/source operations, publication, gates, leases, drivers, rendering | CLI, application services, domain transitions, ports, and adapters | 2 through 9 |
| `state.py` | local/remote Git, source acquisition, CAS publication, pins/owners, leases, candidate gating | Git snapshot/source/retention/change-gate adapters | 4 through 9 |
| `forges.py` | forge discovery, GitHub CLI execution, review workflow | change-gate adapter | 4 |
| `driver.py` and `execution.py` | driver API, installed-kind lookup, filesystem/process execution contexts | driver contract plus driver-host adapter | 2 and 5 |

## Infrastructure dependency inventory

### Git and repository state

- `controller.py` resolves repository roots and refs, invokes Git, materializes
  revisions, performs ancestry checks, and coordinates source/publication state.
- `state.py` exposes Git-shaped refs, revisions, remote-ref snapshots, source
  revisions, publications, pins, owners, leases, and gated candidates.
- `plane_repositories.py` turns plane/ref requests into temporary filesystem
  trees and Git blob IDs.
- `forges.py` invokes Git and GitHub tooling for review candidates.
- `inventory.py`, `inspection.py`, schemas, and drivers consume global registries
  constructed during module import.

These dependencies move behind read capabilities in phase 3, publication and
retention capabilities in phase 4, effect fencing in phase 5, and recovery in
phase 7. The global caches remain compatibility scaffolding until phase 9.

### Filesystem and serialization

- `resource_model.py` contains physical collection roots, YAML/JSON discovery,
  path generation, raw-byte hashing, and blob-ID association alongside semantic
  identity and relationship definitions.
- `inventory.py`, `resources.py`, `operational.py`, and `controller.py` pass
  `Path` values through domain-like operations and driver contexts.
- `formats.py` owns source-authored YAML/JSON loading and project path policy.
- Built-in drivers receive materialized filesystem roots and execute provider
  tools against those roots.

Collection reads move to logical workspaces in phase 3, authored parsing to the
shared specification-input pipeline in phase 4, and driver materialization to
the driver host in phase 5.

### Forge and process execution

- Review policy, candidate refs, GitHub CLI calls, and merge-request behavior
  are visible to orchestration in `controller.py` and `forges.py`.
- Command execution, environment preparation, credentials, and progress output
  are exposed directly to built-in drivers.

Review becomes a change-gate adapter in phase 4. Process and credential access
become driver-host capabilities in phase 5.

## Ambient operation inputs

| Input | Current use | Intended owner | Phase |
| --- | --- | --- | --- |
| wall clock | receipts, operation timestamps, leases and cleanup decisions | application clock; secure retention clock | 2 and 5 |
| UUID/random identifiers | resource UIDs, candidates, leases, pins and ownership records | application identifier factory or secure fencing implementation | 2, 4, and 5 |
| hostname/process/environment | runner evidence, subprocess behavior, credentials and Git identity | execution-identity provider or adapter | 2 and 5 |
| controller evidence | receipts, teardown/finalization and recovery records | authenticated driver results plus application orchestration | 5 and 7 |

Pure transitions must receive these values explicitly. Security-sensitive lease
tokens and generations stay private to the retention/fencing adapter.

## Git-shaped contracts

The coordinated phase-8 cutover must cover all of the following together:

- Project and Environment desired/observed refs, candidate-ref templates,
  change-gate configuration, and effect-lease refs;
- Promotion source/target revisions, ancestry evidence, and accepted heads;
- StackTemplate acquisition requests, repository transports, revisions,
  document digests, and retained source context;
- Unit source repositories/revisions, input hashes, and materialization context;
- Receipt and Artifact desired/observed revisions, source pins, blob identities,
  controller evidence, and inspection provenance;
- driver planning/reconciliation/verification/teardown contexts and results;
- action outputs and CLI flags that expose refs, revisions, repositories,
  candidates, or low-level Git tree/ref operations.

Until that cutover, these wire contracts remain unchanged even when their
implementations move behind ports.

## CLI classification

- Backend-neutral application intents: apply, promote, reconcile, converge,
  inspect/get, validate, rollback, and deletion/finalization workflows.
- Default-Git input translators: repository discovery, branches/tags/revisions,
  working-tree input, candidate-ref selection, and source transports.
- Default-Git administration: low-level tree/ref read, publication, owner/pin,
  lease, and recovery operations.
- Presentation: argument parsing, table/YAML/JSON rendering, progress, styling,
  and exit-code mapping.

Application intents move behind the orchestrator beginning in phase 2. Git
translators and administration remain adapters; presentation remains in the
CLI. Low-level utilities are removed or isolated in phases 8 and 9.

## Characterization baseline

The existing resource-model, address, inventory, Artifact, and inspection tests
already cover authoritative GVK registration, selector/family collisions,
family membership, contribution merging, placements, namespace rules,
relationship endpoints and cycles, driver outputs, and inspection behavior.
Phase 0 adds focused canonical root and mirror address round trips. Phase 1 adds
a synthetic non-GitOpsCtr catalog and a strict kernel import boundary.
