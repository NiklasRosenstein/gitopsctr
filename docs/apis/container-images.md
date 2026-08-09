# ContainerImages

`artifact.gitopsctr.io/v1` `ContainerImages` contains immutable image URIs produced by an `OciImages` unit. The bundled
driver publishes it under the logical name `containers` at `artifacts/<unit>/containers.yaml|json` on the observed ref.

```yaml
apiVersion: artifact.gitopsctr.io/v1
kind: ContainerImages
metadata:
  name: containers
producer:
  apiVersion: unit.gitopsctr.io/v1
  kind: OciImages
  name: application-images
  driverVersion: 1
  sourceRevision: 0123456789abcdef0123456789abcdef01234567
  inputHashVersion: 1
  inputHash: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
images:
  application:
    uri: registry.example.test/application@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
```

Image keys correspond to the names configured by the producing unit. Consumers normally select a digest URI with a
pointer such as `/images/application/uri`.

See the [OCI images unit](../drivers/oci-images.md), the [artifact lookup](artifacts.md#how-fromartifact-resolves), and
the exhaustive [ContainerImages schema](../schemas/apis/artifact.gitopsctr.io/v1/ContainerImages.schema.json).
