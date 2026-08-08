# Kubernetes manifest materialization

`kubernetes-manifests` renders immutable Kubernetes YAML while desired state advances. The rendered bytes and the
resolved unit JSON form one atomic desired-state component:

```text
deploy/dev
├── units/web.json
└── manifests/web/
    └── manifest.yaml
```

The controller records the payload path, media type, digest, renderer metadata, and deterministic resource inventory
in `units/web.json`. Reconciliation, verification, promotion, and rollback consume these committed bytes. Rollback
copies historical bytes and their unit descriptor exactly; it never reruns Helm.

## Rendering

Helm rendering uses the `helm` executable and records its installed version without enforcing one:

```json
{
  "driver": "kubernetes-manifests",
  "source": {"path": "charts/web", "inputs": ["**/*"]},
  "materialize": {
    "type": "helm",
    "releaseName": "web",
    "namespace": "web",
    "values": {"image": {"tag": "current"}},
    "allowSecrets": false
  },
  "delivery": {"mode": "external"}
}
```

Values are resolved after `fromObservation` and `fromPromotion`, so either reference can appear anywhere below
`materialize.values`. Plain rendering copies matching YAML files with stable paths:

```json
{"type": "plain", "paths": ["base/*.yaml", "services/**/*.yml"], "allowSecrets": false}
```

Core `v1/Secret` objects are rejected by default because Git stores the materialized payload. Set
`allowSecrets: true` only when committing those bytes is deliberate and repository access is an acceptable secret
boundary. Symlinks, escaping paths, empty output, duplicate resources, and payload digest mismatches fail loudly.

## Delivery modes

Every Kubernetes unit chooses a delivery mode.

### External

```json
{"delivery": {"mode": "external"}}
```

Advancement publishes manifests and finishes the unit as `MATERIALIZED`. No external action or receipt occurs. This
fits Argo CD or Flux setups that already watch `deploy/<environment>` but do not need the controller to observe them.

### Direct

```json
{
  "delivery": {
    "mode": "direct",
    "kubeContext": "dev",
    "prune": false,
    "wait": [
      {
        "resource": "deployment/web",
        "namespace": "web",
        "condition": "Available",
        "timeoutSeconds": 300
      }
    ]
  }
}
```

Direct delivery uses server-side apply with a stable environment/unit field manager and never forces conflicts. Waits
are explicit; an empty list means API acceptance only. Pruning is off by default. When enabled, the controller applies
and waits first, then deletes only resources found in the previous successful receipt but absent from the new inventory.
`verify` runs a read-only `kubectl diff`.

### Argo CD observed

```json
{
  "delivery": {
    "mode": "external",
    "observer": {
      "type": "argocd",
      "access": "api",
      "application": "web",
      "applicationNamespace": "argocd",
      "argocdContext": "production",
      "timeoutSeconds": 600
    }
  }
}
```

Use `access: "api"` for the `argocd` CLI or `access: "kubernetes"` with `kubeContext` to read the Application CR
through `kubectl`. Authentication is supplied by the environment. A receipt is written only when a single-source
Application reports the exact desired commit, `Synced`, and `Healthy`. The controller never triggers a sync.

## Promotion evidence

Promotion requires receipts by default. An environment containing intentional materialization-only units can opt in:

```json
{"promotionPolicy": {"minimumEvidence": "materialized"}}
```

This accepts `MATERIALIZED` only for units without reconciliation. Any receipt-requiring unit must still be `CLEAN`.
An all-materialized environment needs no observed ref. Promotion renders a fresh target payload from the target
specification and promoted inputs; rollback restores the selected historical desired payload verbatim.

## Local kind acceptance

The repository includes a real Helm/direct-delivery demo. Docker must be running; mise provides the remaining tools.

```console
mise install
mise run sync
mise run kubernetes-demo
mise run kubernetes-demo-clean
```

`mise run kubernetes-acceptance` starts from empty state, renders and applies a ConfigMap to kind, verifies it through
the CLI, proves a second convergence moves no Git refs, and always removes the cluster.
