# JSON Schemas

Typed unit-driver models are the authority for runtime validation and Draft 2020-12 schema generation. Each driver
publishes authored, desired, result, and composed receipt contracts:

- `unit`: authored environment input;
- `desired-unit`: the fully resolved document stored under `deploy/<environment>/units/`;
- `result`: the raw semantic result returned after applying;
- `receipt`: the generic receipt envelope composed with that driver result.

Controller resources use `gitopsctr.io/v1`; unit resources use `unit.gitopsctr.io/v1`. The complete
machine-readable catalog is [`schemas/index.json`](schemas/index.json).

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

`latest` aliases are convenient for discovery, but committed specifications should use pinned versions. The optional
project-level [`gitopsctr.yaml` configuration schema`](project-configuration.md) is published in the same core API group.

## CLI

```console
gitopsctr schemas show terraform receipt
gitopsctr schemas show gitopsctr.io/v1 Environment
gitopsctr schemas show unit.gitopsctr.io/v1/Terraform authored
gitopsctr schemas export docs/schemas
gitopsctr schemas export docs/schemas --check
```

`--check` fails when a current generated document is missing or stale. Historical version directories are retained and
are not removed by export.
