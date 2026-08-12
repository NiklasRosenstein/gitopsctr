# Local Docker tutorial

This tutorial deploys a real HTTP service without cloud credentials. It uses the repository's Docker demo to build an
OCI image, pass its immutable digest to Terraform, and run the resulting container locally.

!!! note "Run from a source checkout"

    You need Docker and [mise](https://mise.jdx.dev/). The demo creates all mutable state below
    `.docker-demo-state/` and never writes deployment refs to the gitopsctr repository or its remote.

## Inspect the authored project

The demo source is under `demo/docker/repository/`:

- `gitopsctr.yaml` declares the `Project` and its environment directory.
- `deployment/environments/dev/environment.yaml` declares the `dev` environment.
- `demo-image.yaml` is an `OciImages` unit that builds and publishes the application image.
- `demo-service.yaml` is a `Terraform` unit that runs the image as a container.

The service obtains the immutable image URI from the image unit:

```yaml
image:
  fromArtifact:
    unit: demo-image
    name: containers
    apiVersion: artifact.gitopsctr.io/v1
    kind: ContainerImages
    pointer: /images/application/uri
```

```mermaid
flowchart LR
  image["demo-image<br/>OciImages"] -->|publishes| artifact["containers artifact<br/>ContainerImages"]
  artifact -->|fromArtifact| service["demo-service<br/>Terraform"]
```

This reference creates a dependency: `demo-image` must publish its receipt and `containers` artifact before
`demo-service` can be resolved.

## Deploy

Install the development tools and start from an empty demo state:

```console
mise install
mise run sync
mise run demo-reset
```

The runner creates an isolated Git repository, starts a local registry, then runs `converge`. Convergence advances
`gitopsctr/desired/dev`, reconciles `demo-image`, publishes its receipt and artifact to `gitopsctr/observed/dev`,
advances the service with the resolved image URI, and finally reconciles `demo-service`.

The command finishes with the application URL and response. Verify it directly:

```console
curl http://127.0.0.1:18080
```

## Inspect desired and observed state

Change into the demo's isolated repository:

```console
cd .docker-demo-state/repository
uv run gitopsctr status --environment dev
uv run gitopsctr list units --environment dev
uv run gitopsctr show desired --environment dev demo-service
uv run gitopsctr show receipt --environment dev demo-image
uv run gitopsctr show receipt --environment dev demo-image --artifact containers
git log --all --oneline --decorate
```

The source commit contains authored resources. `gitopsctr/desired/dev` contains resolved desired units;
`gitopsctr/observed/dev` contains
receipts and artifacts proving what was applied. See [Concepts](concepts.md) for the full state model.

## Source-tracked and directly managed resources

This tutorial uses source-tracked Units: source YAML declares them, and
`advance-desired` controls their lifecycle. Preview CI can also create a
directly managed Stack in the desired ref:

```console
gitopsctr create stack --in=state --or-update \
  --environment preview --name pr-123 --template preview \
  --source-revision "$GITHUB_SHA" \
  --request-id "github:example/application#123:sync:$GITHUB_SHA"
```

The command is safe to retry with the same request ID and inputs. A later
source revision is a new mutation and needs a new request ID. Reusing an ID
with different inputs is rejected. See [Preview environments](preview-environments.md)
for deletion and finalization.

## Prove clean convergence

Return to the source checkout and run the demo again:

```console
cd ../..
mise run demo
```

With unchanged source and external state, no driver runs and neither deployment ref moves. This idempotent second run
is the steady state that `converge` aims for.

Remove the containers, images, registry, isolated Git repository, receipts, and Terraform state when finished:

```console
mise run demo-clean
```

The [demo README](https://github.com/NiklasRosenstein/gitopsctr/tree/main/demo/docker) is the concise operational
reference for reruns, acceptance checks, port overrides, and cleanup.
