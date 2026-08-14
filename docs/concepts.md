# Concepts

gitopsctr separates authored intent, desired state, and observed evidence. Git
records each transition. Unit drivers perform external work.

```mermaid
flowchart LR
  source["Explicit input<br/>authored intent"] -->|apply| desired["Desired ref<br/>gitopsctr/desired/&lt;environment&gt;"]
  desired -->|reconcile| driver["Unit driver"]
  driver --> systems["External systems"]
  driver -->|receipt and artifacts| observed["Observed ref<br/>gitopsctr/observed/&lt;environment&gt;"]
  observed -.->|unlock the next explicit apply| source
```

## Resource definitions and instances

- A **Project** is one source repository containing project configuration and authored environments.
- An **Environment** is gitopsctr's namespace boundary. It selects deployment refs, promotion rules, change gates, and
  its set of resources.
- A **Unit** is a named deployable resource such as an image build, Terraform configuration, or Kubernetes release.
- A **unit driver** implements a unit kind and may support planning, materialization, reconciliation, or verification.
- A **dependency DAG** orders units whose authored values read receipts or artifacts from other units.

gitopsctr's resource registry defines these concepts as invariants rather than scattering them through controller and
CLI code. A **resource family definition** gives a kind or interface its CLI selectors. **Placements** say which
source, desired, or observed representations the family can have, their scope, and their logical collection.
**Relationship definitions** describe how separately stored resources relate, such as a Receipt observing a desired
Unit. Persisted YAML or JSON documents are instances of those definitions; the registry does not create an additional
document format.

The built-in kinds are only the kinds bundled with this distribution. Plugins can register additional Unit and
Artifact kinds by full group/version/kind and participate in the same model. See [Resources and API
kinds](documents.md) for the authoring contracts and the generated [resource model](resource-model.md) for the
authoritative plane and relationship matrix.

## The three storage planes

| Plane | Owner | Typical representations |
| --- | --- | --- |
| Source | User | `Project`, authored `Environment`, Unit, Stack, and StackTemplate resources, plus deployment source files |
| Desired | Controller | Resolved Unit, Stack, StackTemplate, and Promotion representations under `gitopsctr/desired/<environment>` by default |
| Observed | Controller and drivers | Receipt and Artifact resources under `gitopsctr/observed/<environment>` by default |

A resource family may have a representation in more than one plane. For example, Units and Stacks have authored
source and resolved desired representations, while StackTemplates are project-scoped in source and environment-scoped
in desired state. Receipt and Artifact resources live only in the observed plane. Physical Git paths are owned by the
plane's collection adapter; placement is part of the resource definition rather than a controller convention.

An environment may override the desired and observed ref names, but they must remain distinct. Separating them allows
desired state to advance independently while receipts continue to describe the exact desired revision a driver
observed.

## Desired state, receipts, and artifacts

`apply` pins source inputs and resolves available references into an immutable desired unit. A unit is ready
only when all required inputs are available. Materialization-capable drivers may also commit rendered payloads below
`materialized/<unit>/`.

Successful reconciliation writes a **Receipt** to the observed ref. A Receipt is a separate observed resource, not a
Unit's embedded `status`. Its subject identifies a desired Unit, and its desired-unit blob identifies the exact Unit
document that the driver reconciled. Comparing the separately stored documents derives whether the observation is
current or stale; a Unit can also have no Receipt. Raw Unit output therefore remains the exact desired document, while
the default Unit table can join that document with its Receipt to present operational state.

Drivers may publish typed **artifacts** alongside their receipt, for example a `ContainerImages` resource containing
immutable image URIs. Consumers use [reference expressions](references.md) to read receipt results, artifacts, or a
promoted desired unit. The [Receipt](apis/receipt.md) and [artifact](apis/artifacts.md) API pages show how those lookups
follow the desired and observed trees.

## Apply, reconcile, and converge

- **Apply** resolves explicit input and publishes a changed desired snapshot. A named partition makes those roots an
  authoritative set, enabling omission-based pruning; unpartitioned roots are independently applied.
- **Reconcile** plans or applies one desired unit and publishes its receipt after success.
- **Converge** reconciles current desired Units, or repeats apply and reconciliation when explicit input is supplied.
- **Verify** checks supported units for external drift without writing receipts.

Because observations can unlock downstream desired inputs, convergence with explicit input may produce several
desired and observed commits before it becomes clean. Convergence without input re-projects the durable
StackTemplate/Stack intent after evidence changes, while ordinary standalone authored input still requires an
explicit apply when it changes.

## Promotion and rollback

Promotion applies explicit target resources with pinned context from a permitted source environment. It does not
implicitly copy the source desired tree. The resulting controller-owned `Promotion` resource records the exact source
desired, source observed, and target specification revisions.

```mermaid
flowchart LR
  specification["Pinned specification revision<br/>target Environment, Stack, and StackTemplate"] --> promotion["Promotion record<br/>pins three revisions"]
  desired["Pinned source desired revision<br/>resolved source Units and Stacks"] --> promotion
  observed["Pinned source observed revision<br/>fresh receipts and artifacts"] --> promotion
  promotion --> resolution["Resolve target desired state"]
  resolution --> gate{"Target change gate"}
  gate -->|none| target["Target desired ref"]
  gate -->|pullRequest| review["Candidate ref<br/>pull request"]
  review -->|merge| target
```

In compact form:

```text
target desired state = target specification at specificationRevision
                     + selected inputs from source desiredRevision and observedRevision
```

For example, a target Stack may use the directly supplied StackTemplate while importing an exact image artifact
evidenced by the pinned source desired and observed revisions. StackTemplate promotion and external acquisition are
deferred; the current contract does not copy or implicitly acquire a source StackTemplate. See [Stacks and
StackTemplates](apis/stacks.md#desired-state-records) for the direct-inline contract and [Promotion](apis/promotion.md)
for the lineage record.

`changeGate: pullRequest` publishes promotion and rollback candidates for review; `changeGate: none` publishes them
directly. Promotion normally requires every source unit to have a current receipt. Environments that contain only
materialized units may opt into materialized promotion evidence.

Rollback never rewinds a deployment ref. It validates a historical desired snapshot and publishes a new forward
commit containing the selected historical state, preserving an auditable history.
