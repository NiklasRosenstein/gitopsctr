# Local demo

This demo exercises the real reconciliation path without cloud credentials:

1. `demo-image` builds a small HTTP server image and publishes it to a local OCI registry.
2. Its receipt records the immutable image digest on `observed/dev`.
3. `demo-service` consumes that digest through `fromObservation`.
4. Terraform uses the Docker provider to pull the digest and run it on `http://127.0.0.1:18080`.

The runner creates an isolated source repository and bare Git remote under `.demo-state/`. It never writes
deployment refs to the `gitopsctr` repository or its remote.

## Run it

You need Docker running locally. Mise supplies Python, uv, Terraform, ORAS, and the other project tools.

```console
mise install
mise run sync
mise run demo
```

Run `mise run demo` again to demonstrate a clean convergence. Use `mise run demo-reset` after changing the
template or to rebuild from scratch.

Inspect the effects with:

```console
curl http://127.0.0.1:18080
docker ps --filter name=gitopsctr-demo
git -C .demo-state/repository log --all --oneline --decorate
```

Remove the demo's named containers, cached images, local Git remote, receipts, and Terraform state with:

```console
mise run demo-clean
```

## Why this is not the test fixture

`tests/fixtures/repository` is deliberately synthetic: controller tests use it globally and must not require
Docker, Terraform, network access, or mutable external state. The demo mirrors its specification shape but is a
separate runnable repository with real source files and effects.
