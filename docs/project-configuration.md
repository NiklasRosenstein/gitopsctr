# Project configuration

Every GitOpsCTR source tree contains a `Project` resource. It identifies the
project, locates authored environments, and selects the preferred format for generated documents. It can also define
default desired, observed, and candidate ref templates for every environment.

Create it from the root of an existing Git working tree:

```console
gitopsctr create project --name my-project
```

Use `--write-format json` or `--environments-path config/environments` to
select non-default values. The command writes only `gitopsctr.yaml`; create
environments separately after the Project exists.

```yaml
# yaml-language-server: $schema=https://niklasrosenstein.github.io/gitopsctr/schemas/apis/gitopsctr.io/v1/Project.schema.json
apiVersion: gitopsctr.io/v1
kind: Project
metadata:
  name: my-project
spec:
  writeFormat: yaml
  environmentsPath: deployment/environments
  environmentDefaults:
    refs:
      desired: gitopsctr/desired/{environment}
      observed: gitopsctr/observed/{environment}
      candidate: gitopsctr/candidates/{environment}/{id}
  sourceRevisionPolicy:
    unavailableWhen: outside-candidate-history
    whenUnavailableDuringAdvance: refresh
    whenUnavailableDuringPlan: error
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

## Environment ref defaults

`spec.environmentDefaults.refs` defines project-wide ref templates. Every template must contain `{environment}`.
Desired and observed templates support no other placeholders; the candidate template may also contain `{id}` and
`{operation}`.

```yaml
spec:
  environmentDefaults:
    refs:
      desired: deployments/{environment}
      observed: observations/{environment}
      candidate: changes/{environment}/{operation}/{id}
```

The fields are independent. Omitted fields retain the built-ins: `gitopsctr/desired/{environment}`,
`gitopsctr/observed/{environment}`, and `gitopsctr/candidates/{environment}/{id}`. An
[Environment](apis/environment.md#deployment-refs) can replace them, and operation-specific CLI overrides take the
highest priority. Expanded desired and observed refs must differ.

## Source revision refresh policy

`spec.sourceRevisionPolicy` controls when a retained unit source is unavailable and whether an unavailable source is
refreshed or rejected for each operation. It defaults to:

| Field | Default | Behavior |
| --- | --- | --- |
| `unavailableWhen` | `outside-candidate-history` | A retained revision is available only when its commit is an ancestor of the candidate revision. `missing` checks only whether Git can resolve the commit object. |
| `whenUnavailableDuringAdvance` | `refresh` | `advance-desired`, including `advance-desired --dry`, replaces the retained revision with the candidate revision. `error` leaves desired state unchanged. |
| `whenUnavailableDuringPlan` | `error` | `reconcile --plan` fails before invoking the driver. `refresh` uses a refreshed source only in the dry candidate. |

The history check uses `git merge-base --is-ancestor`. A dangling commit that can still be resolved locally is therefore
unavailable under `outside-candidate-history`. Source-less units are not affected. When planning fails because of this
policy, run `advance-desired` from a durable source revision before planning.

Create an environment in that configured location with:

```console
gitopsctr create environment --name dev
gitopsctr create environment --name prod --change-gate pullRequest
```

`gitopsctr create project` writes the built-in templates by default. Use `--desired-ref-template`,
`--observed-ref-template`, and `--candidate-ref-template` to choose different templates while scaffolding the Project.

Use the canonical filename `gitopsctr.yaml`; `.yml`, `.gitopsctr.yaml`, and
`.gitopsctr.yml` are also accepted. A source tree must contain exactly one of
these files. The legacy flat configuration shape is not accepted.

Unknown fields and unsupported values fail before an operation starts. The YAML
language-server directive (or JSON `$schema` property) is an editor hint: it is
not fetched and does not change runtime validation.

The published Draft 2020-12 resource schema is
[`Project.schema.json`](schemas/apis/gitopsctr.io/v1/Project.schema.json), next
to the schemas for the other `gitopsctr.io/v1` kinds.

When migrating legacy documents, supply the required project identity:

```console
python tools/migrate_documents.py --project-name my-project --apply
```
