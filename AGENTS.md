# Contributor contract

## JSON Schema versions

- Typed unit-driver models are authoritative for validation and generated schemas.
- Breaking or narrowing changes to a unit-driver document contract require an API version bump.
- Backward-compatible additions may update the current driver contract.
- Never remove a committed historical schema version from `docs/schemas`.
- Regenerate schemas with `mise run schemas` whenever a model changes.
- Run `mise run check`; CI checks schema freshness and performs a strict MkDocs build.

Core document schemas remain under their document `schema` version. Runtime validation treats `$schema` only as an
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
