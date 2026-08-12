# Project configuration

Every GitOpsCTR source tree contains a `Project` resource. It names the project, locates authored environments, and
selects the format for generated documents. It can also define desired, observed, and candidate ref templates.

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
  stackTemplatesPath: deployment/stack-templates
  effectLease:
    store:
      branch:
        ref: gitopsctr/leases
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
existing representation wins. Changing `writeFormat` does not create a second
copy of a document.

`spec.environmentsPath` is relative to the source-tree root and defaults to
`deployment/environments`. Environment `dev` is read from
`<environmentsPath>/dev/environment.*`, with its authored units in
`<environmentsPath>/dev/units/`. Absolute paths and paths containing `..` are
rejected. Generated desired and observed branches continue to store documents
under top-level `units/`.

`spec.stackTemplatesPath` is relative to the source-tree root and defaults to
`deployment/stack-templates`. A StackTemplate is authored once at project
level and may be selected by Stacks in several environments.

`spec.effectLease` is required. Set it to `null`, or set `store` to `null`, to
disable effect leases. The example keeps lease commits in one shared branch.
Use `{environment}` in the branch ref when each environment needs its own
lease branch. `gitopsctr create project` writes the shared branch form by
default.

## Effect lease storage

An effect lease serializes desired-state changes while a Unit driver runs an
external effect. It records the Unit identity and effect snapshot, and blocks
conflicting changes until the effect releases the lease. A separate lease
branch keeps this coordination history out of the reviewable desired branch.

| Configuration | Behavior |
| --- | --- |
| `effectLease: null` | No effect leases. Use only when external effects are otherwise serialized. |
| `effectLease.store: null` | Same as `effectLease: null`. |
| `store.branch.ref: gitopsctr/desired/{environment}` | Co-locate leases with that environment's desired history. |
| `store.branch.ref: gitopsctr/leases` | Keep leases for all environments in one shared branch. |

The branch ref may contain `{environment}`. Lease recovery is UID- and
token-fenced; use
`recover-effect-lease` only after confirming that the external effect has
stopped.

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

The history check uses `git merge-base --is-ancestor`. Under
`outside-candidate-history`, a local but unreachable commit is unavailable.
Source-less units are not affected. If planning fails, run `advance-desired`
from a durable source revision first.

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

The migration preserves an existing Project configuration. It uses that
configuration to find desired and observed refs and writes canonical
source-tracked metadata for legacy desired Units. It does not retire the
runtime legacy reader; verify every supported desired ref before removing that
compatibility path.
