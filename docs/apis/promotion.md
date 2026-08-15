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
the commit that authenticates the target Project and Environment configuration and the exact bytes of every explicit
target input file. This prevents later branch movement from silently changing either the target specification or
promoted inputs. A Stack-only promotion may reuse a retained target StackTemplate only when no authoritative partition
selects it; when an authoritative partition selects that template, it must be supplied explicitly as target input. A
StackTemplate `source.fromPromotion` selector is another explicit target input: it selects the source StackTemplate
from the pinned source desired revision and is legal only for `promote`, never `apply`. It is not an implicit copy from
the source tree. `observedRevision` may be `null` when the source Environment uses materialized promotion evidence.

| Revision | Supplies |
| --- | --- |
| `spec.source.desiredRevision` | Resolved source Units and Stacks from which promoted values and lineage are selected |
| `spec.source.observedRevision` | Matching receipts and immutable artifacts proving what was reconciled |
| `spec.specificationRevision` | Target Project/Environment configuration and the exact bytes of explicit target inputs |

These revisions are independent. `gitopsctr promote` defaults the source desired revision to the source desired-ref
head and the specification revision to `HEAD`. Use `--specification-revision` when the target must be built from a
specific reviewed commit.

```console
gitopsctr promote \
  --from-environment dev \
  --to-environment staging \
  --file deployment/stack-templates/application.yaml \
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

A target StackTemplate is reused from target desired state only when the promotion input is Stack-only and no
authoritative partition selects the retained template. Otherwise, supply it as inline content or an explicit
`source.fromPromotion` selector with the promotion input. If the selector is used, its resolved acquisition records the
requested Stack name and the source environment, desired ref, exact desired revision, Stack UID, template UID, and
template content digest. The target Stack then references that target desired StackTemplate and can import only the
exact artifact that dev produced:

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

Here, the authoritative `application` partition means the template is supplied explicitly in the target input;
`template: application` resolves that target desired StackTemplate. The artifact import resolves `image/containers` from the source Stack using
the pinned source desired and observed trees, validates its receipt,
producer identity, and digest, and records immutable import lineage in the target desired Stack. Field-level
`fromPromotion` expressions use the same source desired revision to read public source Unit `spec` values.

Repository-backed Unit paths inherit the exact source context retained by the target desired StackTemplate. A Git
acquisition retains the credential-free repository and exact resolved commit in both acquisition lineage and
`sourceContext`; a promoted acquisition carries forward the source template's context. The target can therefore
project those paths again without the original source checkout.

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
