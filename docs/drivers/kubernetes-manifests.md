# Kubernetes manifests unit driver

The `KubernetesManifests` unit driver renders Helm or plain YAML into the
desired tree and can optionally deliver those manifests to a cluster.

**Kind:** `unit.gitopsctr.io/v1/KubernetesManifests`<br>
**Capabilities:** materialization, planning, reconciliation, verification

`kubernetes-manifests` renders immutable Kubernetes YAML while desired state advances. The rendered bytes and the
resolved unit JSON form one atomic desired-state component:

```text
gitopsctr/desired/dev
├── units/web.yaml
└── materialized/web/
    └── manifest.yaml
```

The controller records the payload path, media type, digest, renderer metadata, and deterministic resource inventory
in `units/web.yaml`. Reconciliation, verification, promotion, and rollback consume these committed bytes. Rollback
copies historical bytes and their unit descriptor exactly; it never reruns Helm.

## Rendering

Helm rendering uses the `helm` executable and records its installed version without enforcing one:

```yaml
# yaml-language-server: $schema=https://niklasrosenstein.github.io/gitopsctr/schemas/apis/unit.gitopsctr.io/v1/KubernetesManifests/authored.schema.json
apiVersion: unit.gitopsctr.io/v1
kind: KubernetesManifests
metadata:
  name: web
spec:
  source:
    path: charts/web
    inputs: ["**/*"]
  materialize:
    type: helm
    releaseName: web
    namespace: web
    values:
      image:
        tag: current
    allowSecrets: false
  delivery:
    mode: external
```

Values are resolved after `fromReceipt`, `fromArtifact`, and `fromPromotion`, so any reference can appear anywhere
below `materialize.values`. Plain rendering copies matching YAML files with stable paths:

```yaml
materialize:
  type: plain
  paths: ["base/*.yaml", "services/**/*.yml"]
  allowSecrets: false
```

Core `v1/Secret` objects are rejected by default because Git stores the materialized payload. Set
`allowSecrets: true` only when committing those bytes is deliberate and repository access is an acceptable secret
boundary. Symlinks, escaping paths, empty output, duplicate resources, and payload digest mismatches fail loudly.

## Delivery modes

Every Kubernetes unit chooses a delivery mode.

### External

```yaml
delivery:
  mode: external
```

Apply publishes manifests and finishes the unit as `MATERIALIZED`. No external action or receipt occurs. This
fits Argo CD or Flux setups that already watch `gitopsctr/desired/<environment>` but do not need the controller to
observe them.

### Direct

```yaml
delivery:
  mode: direct
  kubeContext: dev
  prune: false
  wait:
    - resource: deployment/web
      namespace: web
      condition: Available
      timeoutSeconds: 300
```

Direct delivery uses server-side apply with a stable environment/unit field manager and never forces conflicts. Waits
are explicit; an empty list means API acceptance only. Pruning is off by default. When enabled, the controller applies
and waits first, then deletes only resources found in the previous successful receipt but absent from the new inventory.
`verify` runs a read-only `kubectl diff`.

### Argo CD observed

```yaml
delivery:
  mode: external
  observer:
    type: argocd
    access: api
    application: web
    applicationNamespace: argocd
    argocdContext: production
    timeoutSeconds: 600
```

Use `access: "api"` for the `argocd` CLI or `access: "kubernetes"` with `kubeContext` to read the Application CR
through `kubectl`. Authentication is supplied by the environment. A receipt is written only when a single-source
Application reports the exact desired commit, `Synced`, and `Healthy`. The controller never triggers a sync.

An Application can be created before its target desired branch or materialized path exists. During reconciliation,
initial absent status, `Unknown`, and `Missing` health are treated as pending until `timeoutSeconds`; `Degraded` fails
immediately. Verification reports any pending or unhealthy Application as drift.

## Promotion evidence

Promotion requires receipts by default. An environment containing intentional materialization-only units can opt in:

```yaml
promotionPolicy:
  minimumEvidence: materialized
```

This accepts `MATERIALIZED` only for units without reconciliation. Any receipt-requiring unit must still be `CLEAN`.
An all-materialized environment needs no observed ref. Promotion renders a fresh target payload from the target
specification and promoted inputs; rollback restores the selected historical desired payload verbatim.

## Local Kubernetes acceptance

The repository includes a real Stack-based image-build and Helm delivery demo. Docker must be running; mise provides
the remaining tools. kind is the default provider; select minikube with `GITOPSCTR_K8S_PROVIDER=minikube`:

```console
mise install
mise run sync
mise run demo-k8s run
mise run demo-k8s clean
GITOPSCTR_K8S_PROVIDER=minikube mise run demo-k8s run
```

`mise run demo-k8s acceptance` starts from empty state, applies and reconciles the partitioned dev Stack, promotes its exact
image artifact into staging, verifies both workloads, proves clean convergence, and always removes the cluster.
`mise run demo-k8s acceptance --preview` instead applies, updates, and requests deletion of an unpartitioned preview
Stack built from the same template.

Add `--delivery argocd` to any Kubernetes demo command to exercise external delivery. It installs an isolated Argo CD
Core instance, creates automated Applications before their materialized payloads exist, and observes Argo CD syncing
the exact desired revisions. Argo CD, not gitopsctr, applies the workloads.

## Schemas

- [authored unit](../schemas/apis/unit.gitopsctr.io/v1/KubernetesManifests/authored.schema.json)
- [desired unit](../schemas/apis/unit.gitopsctr.io/v1/KubernetesManifests/desired.schema.json)
- [receipt](../schemas/apis/unit.gitopsctr.io/v1/KubernetesManifests/receipt.schema.json)
