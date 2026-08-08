# Vite OCI bundle unit driver

The Vite OCI bundle driver installs dependencies, builds a Vite application,
archives `dist/` deterministically, and publishes the archive as an OCI
artifact. The driver publishes a versioned `FrontendBundle` resource at
`artifacts/<unit>/frontend.yaml` (or `.json`) for downstream units.

**Kind:** `unit.gitopsctr.io/v1/ViteOciBundle`<br>
**Capabilities:** planning, reconciliation

## Authored unit

```yaml
# yaml-language-server: $schema=https://niklasrosenstein.github.io/gitopsctr/schemas/apis/unit.gitopsctr.io/v1/ViteOciBundle/authored.schema.json
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
```

The current contract intentionally pins `build.nodeVersion` to `24`. The
driver runs `npm ci` and `npm run build`, rejects symlinks in the output, and
creates a reproducible tar+gzip layer. The optional credential provider uses
the same AWS ECR shape as the OCI images driver.

Planning performs the local build without publishing. Reconciliation publishes
the immutable artifact and always produces the driver-defined `frontend`
artifact; units do not declare artifact filenames.

```yaml
bundle:
  fromArtifact:
    unit: frontend-bundle
    name: frontend
    apiVersion: artifact.gitopsctr.io/v1
    kind: FrontendBundle
    pointer: /bundle/uri
```

## Schemas

- [authored unit](../schemas/apis/unit.gitopsctr.io/v1/ViteOciBundle/authored.schema.json)
- [desired unit](../schemas/apis/unit.gitopsctr.io/v1/ViteOciBundle/desired.schema.json)
- [receipt](../schemas/apis/unit.gitopsctr.io/v1/ViteOciBundle/receipt.schema.json)
- [FrontendBundle artifact](../schemas/apis/artifact.gitopsctr.io/v1/FrontendBundle.schema.json)
