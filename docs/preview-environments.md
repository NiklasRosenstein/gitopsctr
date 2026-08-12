# Preview environments

Preview environments are controller-owned `Stack` lifecycles. A source-tracked
`StackTemplate` is inert; a concrete `Stack` expands it into owned Units in one
desired snapshot.

Direct instantiation creates a `Stack` root. It does not make a
`StackTemplate` or an arbitrary Unit directly managed. The generated Units are
owned by the Stack and are reconciled and finalized as one lifecycle.

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
gitopsctr instantiate-stack \
  --environment dev \
  --stack pr-123 \
  --template preview \
  --source-revision "$GITHUB_SHA" \
  --parameters '{"namespace":"preview-123","expiresAt":1735689600}' \
  --request-id github:example-org/application#123
```

The request is replay-safe. Keep the same request ID when retrying the same
operation. The source revision must be trusted CI input; a moving ref is
resolved and pinned during instantiation.

Instantiation creates a CAS-fenced controller claim and pins the exact template
revision on a controller-owned ref. The pin remains available while the Stack
and its Units are being torn down and is released only after successful
target finalization. A gated finalization retains a cleanup intent until the
candidate is applied; a later `finalize-stack` retry releases the pin and claim
under their UID-/revision fences. Unclaimed legacy pins remain retained.

When a pull request closes, loses its required label, or expires, trusted CI may
request deletion immediately with `request-delete-direct-stack`. A scheduled CI
job owned by the deployment may recover missed events by enumerating preview
lineage, consulting the forge, and invoking the same UID-fenced deletion and
finalization commands. The core CLI does not inspect forge state, decide
eligibility, detect orphaned previews, or release pins through an out-of-band
path. The Unit finalization commands must complete the owned closure before the
Stack root can be finalized.

### Update and deletion

Save the Stack UID and current desired revision from the instantiation result.
Use both as fences for an update:

```console
gitopsctr update-direct-stack \
  --environment preview \
  --stack pr-123 \
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

When the preview is no longer eligible, request deletion with the current Stack
UID:

```console
gitopsctr request-delete-direct-stack \
  --environment preview \
  --stack pr-123 \
  --uid "$STACK_UID"
```

Reconcile and finalize each owned Unit in reverse dependency order. Then
finalize the Stack root with its deletion generation:

```console
gitopsctr finalize-stack \
  --environment preview \
  --stack pr-123 \
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
- Keep controller pin refs and desired deletion intents observable until the
  Stack closure is finalized; do not manually delete either as a cleanup
  shortcut.
- For an unparseable cleanup root, restore the matching driver and use
  `recover-opaque-unit` when possible. If the external resource was cleaned up
  outside gitopsctr, use `resolve-opaque-unit --uid ... --reason ...
  --confirm-external-cleanup`. The command is UID-fenced and rejects parseable
  roots, active leases, and roots with deletion intents.

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
