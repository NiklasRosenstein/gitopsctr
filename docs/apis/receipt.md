# Receipt

`gitopsctr.io/v1` `Receipt` records the result of reconciling one exact desired unit. The controller and unit driver
write it to `units/<unit>.yaml|json` on the observed ref; users do not author receipts.

```yaml
apiVersion: gitopsctr.io/v1
kind: Receipt
metadata:
  name: infrastructure
spec:
  subject:
    apiVersion: unit.gitopsctr.io/v1
    kind: Terraform
    name: infrastructure
  desired:
    unitBlob: 0123456789abcdef0123456789abcdef01234567
  resolvedInputs: {}
status:
  controller: {}
  result:
    applied:
      sourceRevision: fedcba9876543210fedcba9876543210fedcba98
    outputs:
      api_url: https://api.example.test
```

`spec.subject` identifies the unit kind, `spec.desired.unitBlob` binds the receipt to the serialized desired unit, and
`spec.resolvedInputs` records the reference fingerprints used by that unit. `status.result` follows the subject
driver's typed result contract. Drivers that publish artifacts also add descriptors under `status.artifacts`.

## How `fromReceipt` resolves

```mermaid
flowchart LR
  reference["fromReceipt<br/>unit + pointer"] --> receipt["Observed receipt<br/>units/&lt;unit&gt;"]
  desired["Current desired unit<br/>units/&lt;unit&gt;"] --> freshness{"unitBlob matches?"}
  receipt --> freshness
  freshness -->|yes| result["Validate typed<br/>status.result"]
  result --> pointer["Apply JSON Pointer"]
```

For `fromReceipt: {unit: infrastructure, pointer: /outputs/api_url}`, gitopsctr:

1. Loads `units/infrastructure.*` from the current desired and observed trees.
2. Requires the receipt's `spec.desired.unitBlob` to match the current desired unit blob.
3. Validates the receipt and its `status.result` against the Terraform receipt profile.
4. Applies `/outputs/api_url` relative to `status.result` and fingerprints the receipt blob.

A missing or stale receipt leaves the consumer waiting; an invalid current receipt is an error. `pointer: ""` selects
the whole typed result. See [Reference expressions](../references.md) for `dryFallback` and pointer syntax.

The generic [Receipt schema](../schemas/apis/gitopsctr.io/v1/Receipt.schema.json) describes the envelope. Each
[unit kind](../drivers.md) also publishes a receipt profile that specializes `status.result` and `status.artifacts`.
