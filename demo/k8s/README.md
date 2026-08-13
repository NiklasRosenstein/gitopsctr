# Kubernetes Stack demo

This demo uses one `StackTemplate` to contrast two lifecycle models:

- Normal mode projects a source-tracked Stack in `dev`, then promotes its exact image artifact into a source-tracked
  `staging` Stack.
- `--preview` directly instantiates the same template in the `preview` environment. Acceptance also updates the pinned
  template revision and requests UID-fenced deletion.

The provider comes from `GITOPSCTR_K8S_PROVIDER` and defaults to `kind`:

```console
mise install
mise run sync
mise run demo-k8s run
mise run demo-k8s acceptance
mise run demo-k8s clean

GITOPSCTR_K8S_PROVIDER=minikube mise run demo-k8s run
```

Run the direct preview lifecycle with:

```console
mise run demo-k8s run --preview
mise run demo-k8s acceptance --preview
```

Direct Kubernetes delivery is the default. Select Argo CD external delivery with:

```console
mise run demo-k8s run --delivery argocd
mise run demo-k8s acceptance --delivery argocd
mise run demo-k8s acceptance --preview --delivery argocd
mise run demo-k8s clean --delivery argocd
```

The Argo CD mode installs a pinned, headless Argo CD Core instance and an isolated in-cluster Git daemon. The daemon
is unauthenticated and is suitable only for this disposable local demo. Docker must be running; Mise supplies Helm,
kind, minikube, kubectl, Python, and the other project tools.
