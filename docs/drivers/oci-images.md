# OCI images unit driver

The OCI images driver builds a Docker image once and exports it to one or more named targets. Registry targets
publish immutable digests; kind and minikube targets load the deterministic local tag directly into a cluster. The
driver publishes a versioned `ContainerImages` resource at
`artifacts/<unit>/containers.yaml` (or `.json`) for downstream units.

**Kind:** `unit.gitopsctr.io/v1/OciImages`<br>
**Capabilities:** planning, reconciliation

## Authored unit

```yaml
# yaml-language-server: $schema=https://niklasrosenstein.github.io/gitopsctr/schemas/apis/unit.gitopsctr.io/v1/OciImages/authored.schema.json
apiVersion: unit.gitopsctr.io/v1
kind: OciImages
metadata:
  name: application-images
spec:
  source:
    path: .
    inputs: ["Dockerfile", "src/**/*", "pyproject.toml"]
  build:
    dockerfile: Dockerfile
    platform: linux/amd64
  publish:
    targets:
      application:
        type: registry
        repository: registry.example/application
```

`build.dockerfile` is resolved from the source checkout and `platform` is passed to Docker. `publish.targets` maps
stable artifact names to typed delivery targets. The optional `publish.credentialProvider` currently supports
`{type: aws-ecr}` for registry targets.

Local cluster targets use the kind cluster name or minikube profile:

```yaml
publish:
  targets:
    development:
      type: kind
      cluster: local
    integration:
      type: minikube
      profile: integration
```

The driver builds once per reconciliation. It reuses a matching registry image when possible, then pushes registry
targets and loads local targets. Registry artifacts contain digest-pinned URIs; local artifacts contain the
input-hash-tagged image name loaded into the selected cluster. The driver always produces its `containers` artifact;
units do not declare artifact filenames.

```yaml
image:
  fromArtifact:
    unit: application-images
    name: containers
    apiVersion: artifact.gitopsctr.io/v1
    kind: ContainerImages
    pointer: /images/application/uri
```

Planning builds the image but does not export it. Reconciliation requires all existing registry targets to agree on
the digest; disagreement fails loudly.

## Schemas

- [authored unit](../schemas/apis/unit.gitopsctr.io/v1/OciImages/authored.schema.json)
- [desired unit](../schemas/apis/unit.gitopsctr.io/v1/OciImages/desired.schema.json)
- [receipt](../schemas/apis/unit.gitopsctr.io/v1/OciImages/receipt.schema.json)
- [ContainerImages artifact](../schemas/apis/artifact.gitopsctr.io/v1/ContainerImages.schema.json)
