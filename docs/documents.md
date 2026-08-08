# Documents and unit drivers

GitOpsCTR accepts both YAML and JSON. Every source tree has a Project resource;
YAML is the default write format and a repository can select JSON in its spec:

```yaml
apiVersion: gitopsctr.io/v1
kind: Project
metadata:
  name: example
spec:
  writeFormat: json
```

The file is named `gitopsctr.yaml`; generated desired state, promotions, and
receipts follow its setting. Readers accept either extension. See the
[`Project` resource documentation](project-configuration.md) and its published
[`Project.schema.json`](schemas/apis/gitopsctr.io/v1/Project.schema.json).

Controller-owned resources use `gitopsctr.io/v1`:

```yaml
apiVersion: gitopsctr.io/v1
kind: Environment
metadata:
  name: dev
spec: {}
```

Unit resources use `unit.gitopsctr.io/v1` and the driver kind:

```yaml
apiVersion: unit.gitopsctr.io/v1
kind: Terraform
metadata:
  name: infrastructure
spec:
  source:
    path: infrastructure
```

`source.path` is a POSIX path relative to the root of the selected source
revision. It is not relative to the unit file or its environment directory.
Optional `source.inputs` paths and glob patterns are resolved relative to
`source.path`; absolute paths and `..` are rejected.

## Create and validate authored resources

The create commands write pinned schema hints and follow the Project's
configured environment location and document format:

```console
gitopsctr create project --name example
gitopsctr create environment --name dev
gitopsctr create unit --environment dev --name infrastructure --driver terraform --source-path infrastructure
```

Existing resources are never replaced unless `--force` is explicit. Drivers
may opt into unit creation through `UnitDriver.scaffold_unit_spec()`; every
built-in driver provides a schema-valid starter.

With no target, `validate` checks the Project and every authored environment.
Files and environments can also be selected or combined:

```console
gitopsctr validate
gitopsctr validate --environment dev --environment prod
gitopsctr validate gitopsctr.yaml deployment/environments/dev/units/infrastructure.yaml
gitopsctr validate --environment dev --fail-fast
```

Multi-target validation reports all errors before failing unless
`--fail-fast` is used. Environment and Project validation includes duplicate
representation, resource identity, driver contract, safe-path, and cross-unit
observation checks.

The generic receipt points back to the unit kind:

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
status:
  controller: {}
  result: {}
```

`UnitDriver` is the implementation of a unit kind. Drivers are discovered
through full-GVK entry points, for example
`unit.gitopsctr.io/v1/Terraform`. Capabilities such as planning,
materialization, reconciliation, and verification remain independent traits.
