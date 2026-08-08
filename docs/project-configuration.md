# Project configuration

Every GitOpsCTR source tree contains a `Project` resource. It identifies the
project, locates authored environments, and selects the preferred format for
generated documents.

```yaml
$schema: https://niklasrosenstein.github.io/gitopsctr/schemas/apis/gitopsctr.io/v1/Project.schema.json
apiVersion: gitopsctr.io/v1
kind: Project
metadata:
  name: my-project
spec:
  writeFormat: yaml
  environmentsPath: deployment/environments
```

`metadata.name` is a DNS-1123 project name. `spec.writeFormat` accepts `yaml`
or `json`:

| Value | New files |
| --- | --- |
| `yaml` (default) | `*.yaml` |
| `json` | `*.json` |

Readers accept `.yaml`, `.yml`, and `.json` regardless of this setting. An
existing representation wins, so changing `writeFormat` does not silently
create a second copy of a logical document.

`spec.environmentsPath` is relative to the source-tree root and defaults to
`deployment/environments`. Environment `dev` is read from
`<environmentsPath>/dev/environment.*`, with its authored units in
`<environmentsPath>/dev/units/`. Absolute paths and paths containing `..` are
rejected. Generated desired and observed branches continue to store documents
under top-level `units/`.

Use the canonical filename `gitopsctr.yaml`; `.yml`, `.gitopsctr.yaml`, and
`.gitopsctr.yml` are also accepted. A source tree must contain exactly one of
these files. The legacy flat configuration shape is not accepted.

Unknown fields and unsupported values fail before an operation starts. The
`$schema` value is an editor hint: it is not fetched and does not change runtime
validation.

The published Draft 2020-12 resource schema is
[`Project.schema.json`](schemas/apis/gitopsctr.io/v1/Project.schema.json), next
to the schemas for the other `gitopsctr.io/v1` kinds.

When migrating legacy documents, supply the required project identity:

```console
python tools/migrate_documents.py --project-name my-project --apply
```
