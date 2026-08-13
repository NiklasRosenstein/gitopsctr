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
the source commit containing the target Environment and authored unit specifications. This prevents later branch
movement from silently changing the promoted input. `observedRevision` may be `null` when the source Environment uses
materialized promotion evidence.

During desired-state resolution, [`fromPromotion`](../references.md#promotion-selectors) reads public unit `spec`
values from the pinned source desired revision. Broken selectors in an active Promotion are errors. They do not make
the controller wait.

Promotion resources should not be authored or edited manually. The
[Promotion schema](../schemas/apis/gitopsctr.io/v1/Promotion.schema.json) is the complete structural reference.
