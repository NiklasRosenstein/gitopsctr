# FrontendBundle

`artifact.gitopsctr.io/v1` `FrontendBundle` describes the immutable OCI bundle produced by a `ViteOciBundle` unit. The
bundled driver publishes it under the logical name `frontend` at `artifacts/<qualified-unit>/frontend.yaml|json` on the observed
ref.

```yaml
apiVersion: artifact.gitopsctr.io/v1
kind: FrontendBundle
metadata:
  name: frontend
producer:
  apiVersion: unit.gitopsctr.io/v1
  kind: ViteOciBundle
  name: web-bundle
  driverVersion: 1
  sourceRevision: 0123456789abcdef0123456789abcdef01234567
  inputHashVersion: 1
  inputHash: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
bundle:
  uri: registry.example.test/web@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
  artifactType: application/vnd.gitopsctr.frontend.v1
```

`bundle.uri` is the immutable OCI digest URI. `bundle.artifactType` identifies the archive contract consumed by the
frontend deployment driver. A consumer selects the URI with `/bundle/uri`.

See the [Vite OCI bundle unit](../drivers/vite-oci-bundle.md), the
[artifact lookup](artifacts.md#how-fromartifact-resolves), and the complete
[FrontendBundle schema](../schemas/apis/artifact.gitopsctr.io/v1/FrontendBundle.schema.json).
