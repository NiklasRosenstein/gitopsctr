# Documents and unit drivers

GitOpsCTR accepts both YAML and JSON. YAML is the default write format. A
repository can choose JSON by adding this optional project file:

```yaml
writeFormat: json
```

The file is named `gitopsctr.yaml`; generated desired state, promotions, and
receipts follow its setting. Readers accept either extension.

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
