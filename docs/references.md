# Reference expressions

Reference expressions let one authored value read immutable state produced elsewhere. They are self-contained objects
and can appear in template-bearing fields supported by a unit kind.

!!! note "Two uses of promotion"

    This page describes the field-level `fromPromotion` reference expression, which reads a source Unit's public
    `spec`. A Stack's `artifactImports[].fromPromotion` instead imports a validated artifact from source desired and
    observed state. StackTemplate acquisition has no external Git or source-promotion mode: promotion either reuses a
    target desired StackTemplate or receives it as explicit direct-inline input. See [Stacks and
    StackTemplates](apis/stacks.md#desired-state-records).

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

Receipt and artifact evidence is valid only while its receipt matches the producer's current desired unit. Before a
value is read, the controller checks artifact identity, type, descriptor, media type, and digest. If required evidence
is missing or stale, the consumer waits.

See [Receipt lookup](apis/receipt.md#how-fromreceipt-resolves) and
[Artifact lookup](apis/artifacts.md#how-fromartifact-resolves) for the exact files, freshness checks, and pointer scope.

## Promotion selectors

`fromPromotion` defaults to the target unit name and the containing field's JSON Pointer. Both selectors can be
overridden independently:

| Expression | Source unit | Source pointer |
| --- | --- | --- |
| `fromPromotion: {}` | Target unit name | Containing field path |
| `fromPromotion: {unit: release}` | `release` | Containing field path |
| `fromPromotion: {pointer: /inputs/release}` | Target unit name | `/inputs/release` |
| `fromPromotion: {unit: release, pointer: /inputs/release}` | `release` | `/inputs/release` |

An inferred pointer requires matching `apiVersion` and `kind` values. A cross-kind reference must specify `pointer`.
`pointer: ""` selects the source unit's public `spec`. Resource metadata and driver identity are never exposed.

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

The fallback is used only during dry resolution, including `reconcile --plan` and `apply --dry`, and only when the
reference is unavailable. Normal apply and reconciliation never use it. Invalid expressions, type
errors, and broken active promotions remain fatal. A fallback cannot hide them.

Fallback values use the same recursive template language as their containing field. They may be scalar, structured,
explicitly `null`, or another reference:

```yaml
endpoint:
  fromPromotion:
    dryFallback:
      fromReceipt: {unit: preview-infrastructure, pointer: /outputs/endpoint}
```

`fromParameter` is not allowed anywhere inside a projection-time fallback, including nested fallback objects. Stack
parameters must be expanded before a structural projection is persisted.

When a fallback supplies the value, the unavailable reference contributes no fingerprint. A nested fallback reference
that resolves successfully contributes its own normal fingerprint.
