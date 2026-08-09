# Kubernetes demo

This demo exercises observation-driven convergence without an OCI registry:

1. `demo-image` builds the HTTP application and exports its deterministic local tag to the selected cluster.
2. Its receipt describes the external `containers` artifact on `gitopsctr/observed/dev`.
3. `web` consumes that reference through `fromArtifact`, renders the Helm chart, and applies it directly.
4. The runner waits for the Deployment, calls the application inside the Pod, and runs `gitopsctr verify`.

Select kind or minikube explicitly on every command:

```console
mise install
mise run sync
mise run kubernetes-demo -- kind
mise run kubernetes-demo-clean -- kind

mise run kubernetes-demo -- minikube
mise run kubernetes-demo-clean -- minikube
```

The acceptance flow starts empty, proves the second convergence runs no drivers and moves no refs, and cleans up:

```console
mise run kubernetes-acceptance -- kind
mise run kubernetes-acceptance -- minikube
```

## Argo CD acceptance

The Argo CD variant keeps the same image build and Helm materialization, but it creates an automated Argo CD
Application before `materialized/web` exists. Its first `web --advance` reconciliation commits the rendered YAML to
`gitopsctr/desired/dev` and waits for Argo CD to sync that exact commit. Argo CD applies the workload; gitopsctr only
observes its Application resource through Kubernetes.

```console
mise run argocd-acceptance -- kind
mise run argocd-acceptance -- minikube
```

The test installs a pinned, headless Argo CD Core instance plus an isolated in-cluster Git daemon. Both are removed
with the cluster. The Git daemon is intentionally unauthenticated and is only suitable for this disposable test.

Docker must be running. Mise provides Helm, kind, minikube, kubectl, Python, and the other project tools.
