# Environment

`gitopsctr.io/v1` `Environment` is the user-authored deployment policy for one named environment. It selects Git refs,
change-gate behavior, promotion sources, and promotion evidence. Store it at
`<environmentsPath>/<environment>/environment.yaml|json`; `metadata.name` must match the directory name.

```yaml
# yaml-language-server: $schema=https://niklasrosenstein.github.io/gitopsctr/schemas/apis/gitopsctr.io/v1/Environment.schema.json
apiVersion: gitopsctr.io/v1
kind: Environment
metadata:
  name: staging
spec:
  refs:
    desired: releases/staging
    observed: receipts/staging
    candidate: changes/{environment}/{id}
  changeGate: pullRequest
  promotion:
    allowedSources: [dev]
  promotionPolicy:
    minimumEvidence: reconciled
```

## Deployment refs

`spec.refs.desired` and `spec.refs.observed` are optional exact ref names for this environment. Resolution is
field-by-field:

| Priority | Source |
| --- | --- |
| 1 | Operation-specific CLI `--desired-ref` or `--observed-ref` override |
| 2 | Exact value in `Environment.spec.refs` |
| 3 | Expanded `Project.spec.environmentDefaults.refs` template |
| 4 | Built-in `gitopsctr/desired/<environment>` or `gitopsctr/observed/<environment>` convention |

Project templates replace the literal `{environment}` placeholder. Environment-level desired and observed refs are
literal. The candidate ref stays a template because each proposal can have its own `{id}`. Desired and observed refs
must be non-empty and different. See
[Project configuration](../project-configuration.md#environment-ref-defaults).

### Candidate refs

With `changeGate: pullRequest`, promotions and rollbacks are published to a candidate ref for review. The default
template is `gitopsctr/candidates/{environment}/{id}`. `spec.refs.candidate` can replace the project template for one
environment; an operation-specific `--candidate-ref` exact override has highest priority.

| Placeholder | Meaning |
| --- | --- |
| `{environment}` | Target environment; required |
| `{id}` | Deterministic proposal identifier; optional |
| `{operation}` | `promotion` or `rollback`; optional |

Without `{id}`, all proposals use one candidate branch. An identical retry reuses that branch and pull request. A
different proposal fails while the branch exists. It cannot replace reviewed content.

## Promotion and change gates

- `changeGate: none` publishes promotion and rollback commits directly.
- `changeGate: pullRequest` publishes promotions and rollbacks to a candidate ref for review.
- `promotion.allowedSources` makes the environment promotion-tracked and lists the permitted source environments.
  Without `promotion`, the environment is source-tracked.
- `promotionPolicy.minimumEvidence` defaults to `reconciled`. `materialized` permits promotion when every unit has
  materialized evidence even if no observed ref exists.

Use [`gitopsctr create environment`](../operations.md) for a minimal resource. The
[Environment schema](../schemas/apis/gitopsctr.io/v1/Environment.schema.json) is the complete field reference.
