# Contributor contract

## JSON Schema versions

- Typed unit-driver models are authoritative for validation and generated schemas.
- [DISABLED UNTIL WE ARE IN PRODUCTION] Breaking or narrowing changes to a unit-driver document contract require an API version bump.
- Backward-compatible additions may update the current driver contract.
- [DISABLED UNTIL WE ARE IN PRODUCTION] Never remove a committed historical schema version from `docs/schemas`.
- Until production, keep only the current Kubernetes-style resource schemas under `docs/schemas/apis/<group>/<version>/`;
  remove obsolete generated schemas instead of preserving legacy layouts.
- Regenerate schemas with `mise run schemas` whenever a model changes.
- Run `mise run check`; CI checks schema freshness and performs a strict MkDocs build.

## Commit readiness

- Before committing, run `mise run lint` and `uv run ruff format --check .`; both linting and formatting must pass.
- Run `mise run check` for the full repository verification suite before committing.

Public document schemas are organized by `apiVersion` and `kind`. Runtime validation treats `$schema` only as an
untrusted editor hint and must never fetch it.

## Document formats

- YAML is the preferred authoring and generated-state format; JSON remains accepted.
- A source repository must contain a `gitopsctr.io/v1` `Project` resource in `gitopsctr.yaml`.
- Its spec may set `writeFormat: yaml` or `writeFormat: json` and `environmentsPath`.
- Unit resources use `unit.gitopsctr.io/v1`; controller resources use `gitopsctr.io/v1`.
- `UnitDriver` is the implementation of a unit kind. Entry points use the full GVK.

## Python interfaces

- Do not return heterogeneous positional tuples whose elements are not self-explanatory at call sites, especially
  undocumented tuples. Use a `TypedDict` or dataclass with named fields instead, and document any fields or invariants
  that are not obvious from their names and types.

## Temporary desired-resource migration guard

- Legacy desired Units lacking lifecycle-authority metadata MUST be treated as source-tracked roots until an explicit
  migration or adoption record is durably committed. Never infer direct management, ownership, or deletion from
  missing metadata or an unauthoritative source-path absence. See
  [PREVIEW_ENVIRONMENTS_SPEC.md](PREVIEW_ENVIRONMENTS_SPEC.md) for the lifecycle rationale.
- New desired-state writers MUST NOT emit the legacy shape. Keep its diagnostics and migration coverage until every
  supported desired ref has explicit lifecycle metadata, then remove the compatibility path and this guard.
