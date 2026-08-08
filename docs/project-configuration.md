# Project configuration

The optional `gitopsctr.yaml` file selects the preferred format for documents
that GitOpsCTR writes. YAML is the default when the file is absent. JSON remains
fully supported for reading and writing.

```yaml
$schema: https://niklasrosenstein.github.io/gitopsctr/schemas/apis/gitopsctr.io/v1/ProjectConfig.schema.json
writeFormat: yaml
```

`writeFormat` accepts `yaml` or `json`:

| Value | New files |
| --- | --- |
| `yaml` (default) | `*.yaml` |
| `json` | `*.json` |

Readers accept `.yaml`, `.yml`, and `.json` regardless of this setting. An
existing representation wins, so changing `writeFormat` does not silently
create a second copy of a logical document. Use the canonical filename
`gitopsctr.yaml`; `.yml`, `.gitopsctr.yaml`, and `.gitopsctr.yml` are accepted
when integrating an existing repository.

The configuration is deliberately small. Unknown keys and unsupported format
values fail before an operation starts. The `$schema` value is an editor hint:
it is not fetched and does not change runtime validation.

The published Draft 2020-12 schema is available at
[`ProjectConfig.schema.json`](schemas/apis/gitopsctr.io/v1/ProjectConfig.schema.json).
