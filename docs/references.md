# Reference expressions

Reference expressions let one authored value read immutable state produced elsewhere. They are self-contained objects
and can appear in template-bearing fields supported by a unit kind.

| Expression | Reads | Pointer scope | Required selectors |
| --- | --- | --- | --- |
| `fromReceipt` | Current producer receipt result | Typed driver result | `unit`; `pointer` defaults to `""` |
| `fromArtifact` | Named artifact resource | Complete artifact document | `unit`, `name`, `apiVersion`, `kind`; `pointer` defaults to `""` |
| `fromPromotion` | Desired unit in the pinned promotion source | Source unit's public `spec` | `unit` and `pointer` are independently optional |

```yaml
api_url:
  fromReceipt: {unit: infrastructure, pointer: /outputs/api_url}
image:
  fromArtifact:
    unit: application-images
    name: containers
    apiVersion: artifact.gitopsctr.io/v1
    kind: ContainerImages
    pointer: /images/application/uri
promoted_image:
  fromPromotion: {}
```

Receipt and artifact evidence is usable only while its receipt matches the producer's current desired unit. Artifact
identity, declared type, descriptor, media type, and digest are checked before the value is read. If required evidence
is unavailable or stale, the consumer waits.

## Promotion selectors

`fromPromotion` defaults to the target unit name and the containing field's JSON Pointer. Both selectors can be
overridden independently:

| Expression | Source unit | Source pointer |
| --- | --- | --- |
| `fromPromotion: {}` | Target unit name | Containing field path |
| `fromPromotion: {unit: release}` | `release` | Containing field path |
| `fromPromotion: {pointer: /inputs/release}` | Target unit name | `/inputs/release` |
| `fromPromotion: {unit: release, pointer: /inputs/release}` | `release` | `/inputs/release` |

An inferred pointer requires source and target units to have the same `apiVersion` and `kind`. Cross-kind references
must specify `pointer`. An explicitly authored `pointer: ""` selects the source unit's whole public `spec`; resource
metadata and internal driver identity are never exposed.

Once a promotion is active, a missing source unit, mismatched inferred kind, or unresolved pointer is an error rather
than a waiting condition. Successful promotion inputs are fingerprinted as `<source-unit>#<effective-pointer>`, so
multiple fields from one source unit are tracked independently.

## `dryFallback`

Use `dryFallback` when a bootstrap or speculative plan needs a type-correct value before its promoted, observed, or
artifact evidence exists:

```yaml
image:
  fromArtifact:
    unit: application-images
    name: containers
    apiVersion: artifact.gitopsctr.io/v1
    kind: ContainerImages
    pointer: /images/application/uri
    dryFallback: registry.invalid/application@sha256:0000000000000000000000000000000000000000000000000000000000000000
```

The fallback is considered only during dry resolution, including `reconcile --plan` and `advance-desired --dry`, and
only when the reference is unavailable. Normal advancement and reconciliation never use it. Invalid expressions,
type errors, and broken active promotions remain fatal and cannot be hidden by a fallback.

Fallback values use the same recursive template language as their containing field. They may be scalar, structured,
explicitly `null`, or another reference:

```yaml
endpoint:
  fromPromotion:
    dryFallback:
      fromReceipt: {unit: preview-infrastructure, pointer: /outputs/endpoint}
```

When a fallback supplies the value, the unavailable reference contributes no fingerprint. A nested fallback reference
that resolves successfully contributes its own normal fingerprint.
