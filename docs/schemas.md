# JSON Schemas

Typed resource models are the authority for runtime validation and Draft
2020-12 schema generation. Controller resources use `gitopsctr.io/v1`; unit
resources use `unit.gitopsctr.io/v1`. Each unit kind publishes schemas for:

- `authored`: source input owned by the user;
- `desired`: the fully resolved resource stored under
  `deploy/<environment>/units/`;
- `receipt`: the applied result and controller evidence stored under the
  observed ref.

The complete machine-readable catalog is
[`schemas/index.json`](schemas/index.json).

## Use a pinned schema

Authored documents should point to the exact API schema:

```yaml
$schema: https://niklasrosenstein.github.io/gitopsctr/schemas/apis/unit.gitopsctr.io/v1/Terraform/authored.schema.json
apiVersion: unit.gitopsctr.io/v1
kind: Terraform
metadata:
  name: infrastructure
spec:
  source:
    path: infra
    inputs: ["*.tf"]
  terraform:
    backend:
      path: .state/dev.tfstate
    variables:
      environment: dev
    observeOutputs: []
```

`$schema` helps editors but is never trusted by the runtime: gitopsctr does not fetch it or select validation behavior
from it. Newly generated desired units, promotions, and receipts always contain a canonical pinned URL.

`latest` aliases are convenient for discovery, but committed specifications should use pinned versions. The repository-level
[`Project` resource](project-configuration.md) has a published
[`Project.schema.json`](schemas/apis/gitopsctr.io/v1/Project.schema.json) in the same core API group.

## CLI

```console
gitopsctr schemas show gitopsctr.io/v1 Environment
gitopsctr schemas show gitopsctr.io/v1 Project
gitopsctr schemas show unit.gitopsctr.io/v1/Terraform authored
gitopsctr schemas show unit.gitopsctr.io/v1/Terraform desired
gitopsctr schemas show unit.gitopsctr.io/v1/Terraform receipt
gitopsctr schemas export docs/schemas
gitopsctr schemas export docs/schemas --check
```

`--check` fails when a current generated document is missing or stale. Historical version directories are retained and
are not removed by export.
