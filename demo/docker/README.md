# Local demo

For a guided explanation of the resources, dependency graph, desired and observed refs, and inspection commands, see
the [Local Docker tutorial](../../docs/tutorial.md). This page is the concise run and cleanup reference.

This demo exercises the real reconciliation path without cloud credentials:

1. `demo-image` builds a small HTTP server image and publishes it to a local OCI registry.
2. Its receipt describes the external `containers` artifact on `gitopsctr/observed/dev`.
3. `demo-service` consumes that artifact's immutable digest through `fromArtifact`.
4. Terraform uses the Docker provider to pull the digest and run it on `http://127.0.0.1:18080`.

The runner creates an isolated source repository and bare Git remote under `.docker-demo-state/`. It never writes
deployment refs to the `gitopsctr` repository or its remote.

## Run it

You need Docker running locally. Mise supplies Python, uv, Terraform, ORAS, and the other project tools.

```console
mise install
mise run sync
mise run demo
```

Override the default local ports when necessary:

```console
GITOPSCTR_DEMO_REGISTRY_PORT=5001 GITOPSCTR_DEMO_APP_PORT=18081 mise run demo
```

Run `mise run demo` again to demonstrate a clean convergence. Use `mise run demo-reset` after changing the
template or to rebuild from scratch.

Inspect the effects with:

```console
curl http://127.0.0.1:18080
docker ps --filter name=gitopsctr-demo
git -C .docker-demo-state/repository log --all --oneline --decorate
```

Remove the demo's named containers, cached images, local Git remote, receipts, and Terraform state with:

```console
mise run demo-clean
```

CI runs the same end-to-end acceptance flow available locally. It deploys from an empty state, requires a second
convergence to run no drivers and move no refs, then adds a source Stack, verifies its generated Terraform Unit,
removes the Stack, and finalizes the generated Unit and Stack. It always cleans up:

```console
mise run demo-acceptance
```

The multi-environment Stack acceptance is a separate flow. It creates dev, staging, and template-only preview in a
second isolated repository, builds and publishes R1 and R2 to a private local registry, promotes only dev R1 to
staging, instantiates a direct preview at R1, and checks the exact observed artifact digest against Docker's image and
container inspection. It then proves that staging and preview stay on R1 during the dev-only R2 change, updates the
direct preview to R2, and requests/finalizes its UID-fenced deletion:

```console
mise run demo-preview-acceptance
```

Prerequisites are Docker running locally plus the Mise-managed Python, Terraform, and project dependencies. The exact
ports are registry `5003`, dev `18180`, staging `18181`, and preview `18182`; override the registry port with
`GITOPSCTR_PREVIEW_REGISTRY_PORT` if needed. Cleanup is scoped to `.docker-preview-acceptance-state/`, the
`gitopsctr-preview-acceptance-registry` container, and the three `gitopsctr-preview-*` containers.

The flow uses the agreed explicit command, including the UID and desired-head fences:

```console
gitopsctr update-direct-stack --environment preview --stack preview --template application \
  --uid STACK_UID --desired-revision DESIRED_REVISION --source-revision REVISION \
  --parameters JSON --request-id demo:gitopsctr-preview#1
```

The acceptance reports a clear API mismatch if the selected controller build does not provide this command rather than
silently replacing the update with a different operation. Do not use this command in the short `mise run demo`.

The separate [`kubernetes`](../kubernetes/) demo builds an image, exports it to a selected kind or minikube cluster,
renders a real Helm chart, and verifies the deployed application. See its README for commands and the
[Kubernetes unit driver documentation](../../docs/drivers/kubernetes-manifests.md) for configuration and cleanup details.

## Why this is not the test fixture

`tests/fixtures/repository` is deliberately synthetic: controller tests use it globally and must not require
Docker, Terraform, network access, or mutable external state. The demo mirrors its specification shape but is a
separate runnable repository with real source files and effects.
