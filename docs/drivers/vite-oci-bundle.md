# Vite OCI bundle unit driver

The Vite OCI bundle driver installs dependencies, builds a Vite application,
archives `dist/` deterministically, and publishes the archive as an OCI
artifact. The resulting `frontend.json` document exposes the immutable bundle
URI to downstream units.

**Kind:** `unit.gitopsctr.io/v1/ViteOciBundle`<br>
**Version:** `v1`<br>
**Capabilities:** planning, reconciliation

## Authored unit

```yaml
$schema: https://niklasrosenstein.github.io/gitopsctr/schemas/apis/unit.gitopsctr.io/v1/ViteOciBundle/authored.schema.json
apiVersion: unit.gitopsctr.io/v1
kind: ViteOciBundle
metadata:
  name: frontend-bundle
spec:
  source:
    path: web
    inputs: ["**/*"]
  build:
    nodeVersion: "24"
  publish:
    repository: registry.example/frontend
  artifacts: [frontend.json]
```

The current contract intentionally pins `build.nodeVersion` to `24`. The
driver runs `npm ci` and `npm run build`, rejects symlinks in the output, and
creates a reproducible tar+gzip layer. The optional credential provider uses
the same AWS ECR shape as the OCI images driver.

Planning performs the local build without publishing. Reconciliation publishes
the immutable artifact and returns its digest and artifact type.

## Schemas

- [authored unit](../schemas/drivers/vite-oci-bundle/v1/unit.schema.json)
- [desired unit](../schemas/drivers/vite-oci-bundle/v1/desired-unit.schema.json)
- [result](../schemas/drivers/vite-oci-bundle/v1/result.schema.json)
- [receipt](../schemas/drivers/vite-oci-bundle/v1/receipt.schema.json)
