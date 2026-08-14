# Promotion

`gitopsctr.io/v1` `Promotion` is controller-owned lineage stored as `promotion.yaml|json` at the root of a promoted
desired ref. Users configure promotion policy in the target [Environment](environment.md); `gitopsctr promote` writes
this resource.

```yaml
apiVersion: gitopsctr.io/v1
kind: Promotion
metadata:
  name: dev
spec:
  source:
    environment: dev
    desiredRef: gitopsctr/desired/dev
    desiredRevision: 0123456789abcdef0123456789abcdef01234567
    observedRef: gitopsctr/observed/dev
    observedRevision: 89abcdef0123456789abcdef0123456789abcdef
  specificationRevision: fedcba9876543210fedcba9876543210fedcba98
```

The source block pins the exact desired and observed histories reviewed for promotion. `specificationRevision` pins
the source commit containing the target Environment, project configuration, and template sources. Explicit `--file`
inputs select the target resources to apply. This prevents later branch movement from
silently changing either the target specification or promoted inputs. `observedRevision` may be `null` when the source
Environment uses materialized promotion evidence.

| Revision | Supplies |
| --- | --- |
| `spec.source.desiredRevision` | Resolved source Units and Stacks from which promoted values and lineage are selected |
| `spec.source.observedRevision` | Matching receipts and immutable artifacts proving what was reconciled |
| `spec.specificationRevision` | Target Environment, project configuration, and StackTemplate sources used by the explicit input |

These revisions are independent. `gitopsctr promote` defaults the source desired revision to the source desired-ref
head and the specification revision to `HEAD`. Use `--specification-revision` when the target must be built from a
specific reviewed commit.

```console
gitopsctr promote \
  --from-environment dev \
  --to-environment staging \
  --file deployment/environments/staging/stacks/application.yaml \
  --partition application \
  --specification-revision REVIEWED_SHA
```

The supplied resources are the only target roots constructed by the operation. With `--partition`, they are also the
complete authoritative membership of that partition, so omitted previous members begin deletion.

## How target desired state is built

Promotion is a resolution context, not an instruction to copy the source desired tree:

```text
target desired state = target specification at specificationRevision
                     + selected inputs from source desiredRevision and observedRevision
```

The target Environment opts into promotion and permits the source:

```yaml
apiVersion: gitopsctr.io/v1
kind: Environment
metadata:
  name: staging
spec:
  promotion:
    allowedSources: [dev]
  promotionPolicy:
    minimumEvidence: reconciled
```

A target Stack can use its parameterized StackTemplate from `specificationRevision` and import only the exact artifact
that dev produced:

```yaml
apiVersion: gitopsctr.io/v1
kind: Stack
metadata:
  name: application
spec:
  template: application
  parameters:
    workload-name: application-staging
    message: promoted from dev to staging
  units: [deploy]
  artifactImports:
    - unit: image
      name: containers
      apiVersion: artifact.gitopsctr.io/v1
      kind: ContainerImages
      fromPromotion:
        stack: application
```

Here, `template: application` resolves the template from the pinned specification tree. The artifact import resolves
`image/containers` from the source Stack using the pinned source desired and observed trees, validates its receipt and
digest, and records immutable import lineage in the target desired Stack.

`template.source.fromPromotion` makes a different, independent selection: it follows the source Stack's recorded
StackTemplate commit, path, and digest, verifies that exact parameterized document, and expands it with the target
Stack's parameters and Unit selection. It does not reuse the source Stack's expanded projection. A target Stack is not
required to choose this mode merely because its Environment permits promotion. See [Promotion and template
selection](stacks.md#promotion-and-template-selection).

During desired-state resolution, [`fromPromotion`](../references.md#promotion-selectors) reads public unit `spec`
values from the pinned source desired revision. Broken selectors in an active Promotion are errors. They do not make
the controller wait.

Promotion resources should not be authored or edited manually. The
[Promotion schema](../schemas/apis/gitopsctr.io/v1/Promotion.schema.json) is the complete structural reference.

Inspect Promotion lineage on the target desired ref with:

```console
gitopsctr get promotions --environment staging
gitopsctr get promotion dev --environment staging -o yaml
```

The table summarizes the source and pinned revisions. YAML or JSON returns the exact persisted Promotion resource.
Use `gitopsctr get promotions -A` to compare target Environments.
