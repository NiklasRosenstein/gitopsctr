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
# yaml-language-server: $schema=https://niklasrosenstein.github.io/gitopsctr/schemas/apis/unit.gitopsctr.io/v1/Terraform/authored.schema.json
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

The YAML language-server directive helps editors but is never trusted by the runtime: gitopsctr does not fetch it or
select validation behavior from it. Newly generated YAML resources use the directive; JSON resources contain the same
canonical pinned URL in their `$schema` property.

The repository-level [`Project` resource](project-configuration.md) has a published
[`Project.schema.json`](schemas/apis/gitopsctr.io/v1/Project.schema.json) in the controller API group.

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

`--check` fails when a generated document is missing, stale, or obsolete. Until the API reaches production, export keeps
only the current resource schemas and removes superseded generated files.
