# Preview environments

A controller-owned `Stack` is one recommended model for a preview environment.
A source-tracked `StackTemplate` is inert; a concrete `Stack` expands it into
owned Units in one desired snapshot. A preview can also contain one or more
directly managed Units without a Stack.

Direct instantiation creates a `Stack` root. It does not make a
`StackTemplate` or an arbitrary Unit directly managed. The generated Units are
owned by the Stack and are reconciled and finalized as one lifecycle.

## Resource commands

Source and desired state have different workflows:

| Command | Target | Purpose |
| --- | --- | --- |
| `create <kind> --in=source` | Project files | Scaffold an authored resource. |
| `create stack --in=state` | Desired ref | Create a direct Stack from a trusted template revision. |
| `apply stack|unit --in=state` | Desired ref | Create or update a direct resource. |
| `delete <kind> --in=source` | Project files | Remove an authored resource; Git records the change. |
| `delete <kind> --in=state` | Desired ref | Request UID-fenced lifecycle deletion. |

There is no source `apply` operation. Edit existing source YAML and commit it;
`advance-desired` resolves that source into desired state. `create` is a
scaffolding convenience, while `apply` is the state mutation API.

## Request IDs

`--request-id` is a caller-defined idempotency key for one logical state
mutation. Retry the same mutation with the same value. Use a new value for a
new mutation. Reusing a value with different inputs is rejected.

CI delivery is at least once: a job may lose the response after the mutation
was accepted and send the request again. The request ID lets `gitopsctr`
recognize that retry as the same logical mutation instead of treating it as a
new one. UID and desired-revision fences still protect against stale or
concurrent resource updates; the request ID handles retry identity.

The value is opaque to `gitopsctr`. An integration may use a convention such
as `github:example/application#123:sync:abc123`; `gitopsctr` does not parse or
validate its meaning.

PR CI can use `create stack --in=state --or-update` for both the first
creation and later updates. Use the same request ID when retrying one CI run,
and a new request ID for a later source revision. Stack deletion uses the
Stack UID as its lifecycle fence.

For a one-Unit preview, `create unit --in=state` or `apply unit --in=state`
takes a canonical desired Unit document with direct lifecycle metadata. The
Unit is a direct root and can later be removed with `delete unit --in=state`.

## Authored layout

Authored resources use the project-level StackTemplate path and the
environment-level Stack path:

```text
deployment/
├── environments/
│   └── dev/
│       ├── environment.yaml
│       └── stacks/
│           └── application.yaml
└── stack-templates/
    └── preview.yaml
```

The generated desired ref stores the corresponding roots under
`stack-templates/` and `stacks/`, and generated Units under `units/`. A
StackTemplate resource may declare `dependsOn` for generated Units. Those edges
control convergence and reverse teardown order; receipt and artifact references
remain separate dependency edges. Generated Unit names are scoped to the
concrete Stack, for example `web--preview-app`, so multiple Stack instances can
use one template in the same desired ref.

See [Project configuration](project-configuration.md) for the path settings
and effect-lease policy.

## Effect lease policy

An effect lease protects an external Unit effect from a conflicting desired
state change. The lease stores the Unit identity and effect snapshot, and is
renewed while the driver runs. A stale lease cannot be recovered without its
exact token and confirmation that the effect stopped.

Use `effectLease: null` or `effectLease.store: null` to disable leases. Use
`gitopsctr/desired/{environment}` to co-locate leases with desired history, or
`gitopsctr/leases` to keep coordination commits in one shared branch. A branch
ref may contain `{environment}`. `gitopsctr create project` selects the shared
branch form by default.

The acceptance test runs once with the default branch store and once with
leases disabled. This verifies both coordination behavior and the no-lease
path.

## Direct preview workflow

The workflow must use a trusted source revision and a stable request identity.
The identity is provenance only; it may be an opaque CI-generated value or a
forge reference such as `github:OWNER/REPOSITORY#NUMBER`. Include an `expiresAt`
Unix timestamp in the Stack parameters when the preview has a time limit:

```console
gitopsctr create stack \
  --in=state \
  --environment dev \
  --name pr-123 \
  --template preview \
  --units image,deploy \
  --source-revision "$GITHUB_SHA" \
  --parameters '{"namespace":"preview-123","expiresAt":1735689600}' \
  --request-id github:example-org/application#123
```

PR CI can use `create stack --in=state --or-update` for every synchronization.
The command creates an absent Stack and updates an existing direct Stack. The
generic form is `apply stack --in=state`; both forms use the same UID,
desired-head, and request-identity fences.

The request is replay-safe. Keep the same request ID when retrying the same
operation. The source revision must be trusted CI input; a moving ref is
resolved and pinned during instantiation.

Instantiation creates a CAS-fenced controller claim and pins the exact template
revision on a controller-owned ref. The pin remains available while the Stack
and its Units are being torn down and is released only after successful
target finalization. A gated finalization retains the deletion-marked resource
until the candidate is applied; a later `finalize` retry releases the pin and
claim under their UID-/revision fences. Unclaimed legacy pins remain retained.

When a pull request closes, loses its required label, or expires, trusted CI may
request deletion immediately with `delete stack --in=state`. A scheduled CI
job owned by the deployment may recover missed events by enumerating preview
lineage, consulting the forge, and invoking the same UID-fenced deletion and
finalization commands. The core CLI does not inspect forge state, decide
eligibility, detect orphaned previews, or release pins through an out-of-band
path. The finalization commands must complete the owned closure before the
Stack root can be finalized.

### Update and deletion

Save the Stack UID and current desired revision from the instantiation result.
Use both as fences for an update:

```console
gitopsctr apply stack \
  --in=state \
  --environment preview \
  --name pr-123 \
  --uid "$STACK_UID" \
  --desired-revision "$DESIRED_REVISION" \
  --template preview \
  --source-revision "$GITHUB_SHA" \
  --parameters '{"namespace":"preview-123","expiresAt":1735689600}' \
  --request-id github:example-org/application#123:refresh
```

If an input artifact is not observed yet, advance and reconcile the producer,
then retry the update with a new request ID. The update preserves the Stack and
child Unit UIDs.

### Deletion metadata

Deletion is recorded on the retained desired resource:

```yaml
metadata:
  ownerReferences:
    - apiVersion: gitopsctr.io/v1
      kind: Stack
      name: pr-123
      uid: <stack-uid>
  deletion:
    generation: 1
    resourceDigest: sha256:<digest>
```

`ownerReferences` identifies the UID-fenced owner. `deletion` marks the
resource for terminal deletion and protects its contents while cleanup runs.
Children are found from their owner references and finalized before the owner.
The deletion generation and resource digest are required finalization fences.
A root keeps `metadata.lifecycle.management` and does not use
`ownerReferences`.

When the preview is no longer eligible, request deletion with the current Stack
UID:

```console
gitopsctr delete stack \
  --in=state \
  --environment preview \
  --name pr-123 \
  --uid "$STACK_UID"
```

Reconcile and finalize each owned Unit in reverse dependency order. Then
finalize the Stack root with its deletion generation:

```console
gitopsctr finalize unit \
  --environment preview \
  --name pr-123--deploy \
  --uid "$UNIT_UID" \
  --deletion-generation "$UNIT_DELETION_GENERATION"
```

```console
gitopsctr finalize stack \
  --environment preview \
  --name pr-123 \
  --uid "$STACK_UID" \
  --deletion-generation "$STACK_DELETION_GENERATION"
```

Do not delete desired documents or controller pins manually. If an operation
is gated or interrupted, repeat it with the same UID and generation fences
after the required candidate or observation is available.

## Argo CD boundary

The preferred integration uses one trusted, long-lived ApplicationSet and a
pull-request generator. The generator and the Stack workflow must use the same
eligibility label and expiry policy:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: previews
  namespace: argocd
spec:
  syncPolicy:
    applicationsSync: sync
    preserveResourcesOnDeletion: false
  generators:
    - pullRequest:
        github:
          owner: example-org
          repo: application
          labels:
            - preview
        requeueAfterSeconds: 180
  template:
    metadata:
      name: 'preview-{{number}}'
    spec:
      project: previews
      source:
        repoURL: https://github.com/example-org/application-deployment.git
        targetRevision: gitopsctr/preview-manifests
        path: previews/{{number}}
      destination:
        server: https://kubernetes.default.svc
        namespace: 'preview-{{number}}'
      syncPolicy:
        automated: {}
        syncOptions:
          - CreateNamespace=true
```

The ref and path templates depend on the deployment. The ApplicationSet and
gitopsctr must use the same eligibility decision.
`CreateNamespace=true` does not by itself establish cleanup ownership, so a
dedicated Namespace manifest should be included when namespace deletion is
part of the preview contract.

The Argo-backed Kubernetes Unit waits until the Application is absent. The
Kubernetes/Argo acceptance job checks external delivery and observation. This
repository does not publish preview manifests or own ApplicationSet resources.
External effects not represented by Kubernetes resources must use Unit teardown,
such as Terraform destroy or a `PostDelete` hook whose completion is observable
to the controller.

## Trust and operations

- The workflow, StackTemplate, deployment policy, and target revision are
  trusted control-plane inputs.
- A `pull_request_target` workflow must not execute pull-request-authored code
  or configuration.
- Pull-request-authored Terraform is arbitrary code; isolate it and scope its
  credentials explicitly.
- Verify desired state and the UID before every deletion request. A stale UID
  must stop cleanup; forge eligibility is the responsibility of the external CI
  orchestrator.
- Keep controller pin refs and deletion-marked desired resources observable
  until the Stack closure is finalized; do not manually delete either as a
  cleanup shortcut.
- For an unparseable cleanup root, restore the matching driver and use
  `recover-opaque-unit` when possible. If the external resource was cleaned up
  outside gitopsctr, use `resolve-opaque-unit --uid ...
  --deletion-generation ... --reason ... --confirm-external-cleanup`. The command
  is UID- and deletion-generation-fenced and rejects parseable roots, active
  leases, and roots that are not marked for deletion.

## Compatibility audit

Before retiring legacy desired-Unit handling, audit each supported desired ref:

```console
gitopsctr audit-desired-compatibility \
  --environment dev \
  --desired-ref deploy/dev
```

For all environments configured by the Project, use the aggregate report:

```console
gitopsctr audit-desired-compatibility --all
```

The command is read-only. It always prints versioned JSON. A clean ref has
`"clean": true` and no findings. Legacy or partial Units, invalid resource
graphs, ambiguous cleanup state, unverified deletion identities, and opaque
cleanup roots produce findings and a non-zero exit status. Run it for every
supported desired ref, or use `--all` to audit the complete Project-configured
inventory, before removing compatibility handling. Duplicate desired refs and
unavailable environment configuration also fail the aggregate audit.
