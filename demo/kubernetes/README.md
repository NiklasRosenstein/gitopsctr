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

Docker must be running. Mise provides Helm, kind, minikube, kubectl, Python, and the other project tools.
