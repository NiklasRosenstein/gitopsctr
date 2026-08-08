# Terraform unit driver

The Terraform unit driver runs `terraform init`, creates a plan, and applies
that plan during reconciliation. It can also run HTTP checks against observed
Terraform outputs and supports a refresh-enabled read-only verification.

**Kind:** `unit.gitopsctr.io/v1/Terraform`<br>
**Capabilities:** planning, reconciliation, verification

## Authored unit

```yaml
$schema: https://niklasrosenstein.github.io/gitopsctr/schemas/apis/unit.gitopsctr.io/v1/Terraform/authored.schema.json
apiVersion: unit.gitopsctr.io/v1
kind: Terraform
metadata:
  name: infrastructure
spec:
  source:
    path: infra
    inputs: ["**/*.tf", "*.tfvars"]
  terraform:
    backend:
      key: example/dev.tfstate
    variables:
      environment: dev
    observeOutputs: [service_url]
    checks:
      - type: http
        urlOutput: service_url
        path: /health
```

`source.path` identifies the Terraform working directory. `source.inputs` is
the input fingerprint; glob patterns are supported. `backend` is passed to
`terraform init`, `variables` become `TF_VAR_*` values, and `observeOutputs`
selects outputs that downstream units may consume. HTTP checks are optional and
run after apply. Backend and variable values must be JSON-compatible scalar or
object values accepted by the driver.

`reconcile --plan` runs the speculative plan and writes report evidence only;
it does not apply or publish a receipt. `verify` runs a refresh-enabled,
read-only plan and reports `CLEAN` or `DRIFT`.

## Schemas

- [authored unit](../schemas/apis/unit.gitopsctr.io/v1/Terraform/authored.schema.json)
- [desired unit](../schemas/apis/unit.gitopsctr.io/v1/Terraform/desired.schema.json)
- [receipt](../schemas/apis/unit.gitopsctr.io/v1/Terraform/receipt.schema.json)
