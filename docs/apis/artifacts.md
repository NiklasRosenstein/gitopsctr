# Artifact resources

Artifact resources are immutable, typed outputs published by unit drivers on the observed ref. A producer writes each
artifact to `artifacts/<unit>/<logical-name>.yaml|json` and records its GVK, path, media type, and digest in the unit's
[Receipt](receipt.md).

The current bundled artifact kinds are:

| Kind | Producer | Logical name | Documentation |
| --- | --- | --- | --- |
| `ContainerImages` | `OciImages` | `containers` | [Container images](container-images.md) |
| `FrontendBundle` | `ViteOciBundle` | `frontend` | [Frontend bundle](frontend-bundle.md) |

Plugins can register additional `artifact.gitopsctr.io` kinds. The table describes the bundled set, not a complete
ecosystem.

## How `fromArtifact` resolves

```mermaid
flowchart LR
  reference["fromArtifact selectors"] --> receipt["Fresh producer receipt"]
  receipt --> descriptor["status.artifacts[name]"]
  descriptor --> artifact["Typed artifact document"]
  desired["Current producer desired unit"] --> artifact
  artifact --> pointer["Apply JSON Pointer<br/>to the whole resource"]
```

```yaml
image:
  fromArtifact:
    unit: application-images
    name: containers
    apiVersion: artifact.gitopsctr.io/v1
    kind: ContainerImages
    pointer: /images/application/uri
```

The lookup first requires a current producer receipt. The driver must declare the logical artifact name and the same
GVK selected by the reference. gitopsctr then follows the receipt descriptor, verifies its required path, media type,
digest, and GVK, parses the registered artifact contract, and checks its producer identity against the current desired
unit. Only then is the pointer applied to the complete artifact resource.

Missing, stale, or integrity-mismatched evidence leaves the consumer waiting. Invalid reference syntax, an unregistered
artifact GVK, or a malformed typed artifact document is an error. The artifact digest is the resolved-input
fingerprint. See [Reference expressions](../references.md) for selector syntax and `dryFallback`.
