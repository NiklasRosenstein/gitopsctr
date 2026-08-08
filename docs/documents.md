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
