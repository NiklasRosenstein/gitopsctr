# OCI images unit driver

The OCI images driver builds one or more Docker images and publishes immutable
digests to named OCI repositories. The same input hash produces the same tag,
which makes retries idempotent and lets downstream units consume
`containers.json` through observations.

**Kind:** `unit.gitopsctr.io/v1/OciImages`<br>
**Version:** `v2`<br>
**Capabilities:** planning, reconciliation

## Authored unit

```yaml
$schema: https://niklasrosenstein.github.io/gitopsctr/schemas/apis/unit.gitopsctr.io/v1/OciImages/authored.schema.json
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
    repositories:
      application: registry.example/application
  artifacts: [containers.json]
```

`build.dockerfile` is resolved from the source checkout and `platform` is
passed to Docker. `publish.repositories` maps stable artifact names to OCI
repositories. The optional `publish.credentialProvider` currently supports
`{type: aws-ecr}`. The driver emits `containers.json`, containing immutable
image URIs and the source/input identity.

Planning builds the image but does not publish it. Reconciliation reuses an
existing digest when all named repositories agree; disagreement fails loudly.

## Schemas

- [authored unit](../schemas/drivers/oci-images/v2/unit.schema.json)
- [desired unit](../schemas/drivers/oci-images/v2/desired-unit.schema.json)
- [result](../schemas/drivers/oci-images/v2/result.schema.json)
- [receipt](../schemas/drivers/oci-images/v2/receipt.schema.json)
