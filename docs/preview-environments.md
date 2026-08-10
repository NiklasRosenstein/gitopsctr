# Preview environments

Preview environments are controller-owned `Stack` lifecycles. A source-tracked
`StackTemplate` is inert; a concrete `Stack` expands it into owned Units in one
desired snapshot.

## Authored layout

Authored resources live below the environment:

```text
deployment/environments/dev/
├── environment.yaml
├── stack-templates/
│   └── preview.yaml
└── stacks/
    └── application.yaml
```

The generated desired ref stores the corresponding roots under
`stack-templates/` and `stacks/`, and generated Units under `units/`. A
StackTemplate resource may declare `dependsOn` for generated Units. Those edges
control convergence and reverse teardown order; receipt and artifact references
remain separate dependency edges. Generated Unit names are scoped to the
concrete Stack, for example `web--preview-app`, so multiple Stack instances can
use one template in the same desired ref.

## Direct preview workflow

The workflow must use a trusted source revision and a stable request identity.
For GitHub, use `github:OWNER/REPOSITORY#NUMBER` or the canonical pull-request
URL. Include an `expiresAt` Unix timestamp in the Stack parameters when the
preview has a time limit:

```console
gitopsctr instantiate-stack \
  --environment dev \
  --stack pr-123 \
  --template preview \
  --source-revision "$GITHUB_SHA" \
  --parameters '{"namespace":"preview-123","expiresAt":1735689600}' \
  --request-id github:example-org/application#123
```

Instantiation pins the exact template revision on a controller-owned ref. The
pin remains available while the Stack and its Units are being torn down and is
released only after successful `finalize-stack` publication.

When a pull request closes, loses its required label, or expires, a webhook may
request deletion immediately. A scheduled job can recover missed events:

```console
gitopsctr recover-orphaned-stacks \
  --environment dev \
  --required-label preview
```

The recovery operation is fail-closed when forge state is unknown. It creates a
normal UID-fenced Stack deletion intent; it never deletes external resources or
releases a pin through an out-of-band path. The Unit finalization commands must
complete the owned closure before the Stack root can be finalized.

GitHub eligibility is read through `gh pr view`. GitLab.com eligibility is read
through `glab mr view`. Both consider a request eligible only while it is open
and, when configured, carries the required label. Self-hosted GitLab and
deployment-specific merge-request creation still require an external adapter;
the controller must not infer eligibility from an opaque request identity.

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

The exact ref/path templating is deployment-specific; the important boundary
is that the ApplicationSet and gitopsctr use the same eligibility decision.
`CreateNamespace=true` does not by itself establish cleanup ownership, so a
dedicated Namespace manifest should be included when namespace deletion is
part of the preview contract.

The deployment adapter must wait for the Application and its workloads to be
absent before invoking Stack finalization. This repository currently documents
that handshake but does not publish or observe Argo Applications itself.
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
  or unknown forge response must stop cleanup.
- Re-run `recover-orphaned-stacks` after a transient forge or Git failure. It is
  idempotent for the same Stack and request identity.
- Keep controller pin refs and desired deletion intents observable until the
  Stack closure is finalized; do not manually delete either as a cleanup
  shortcut.
