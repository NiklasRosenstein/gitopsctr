# Contributor contract

## JSON Schema versions

- Typed plugin models are authoritative for validation and generated schemas.
- Breaking or narrowing changes to a plugin document contract require a `UnitPlugin.version` bump.
- Backward-compatible additions may update the current plugin version.
- Never remove a committed historical schema version from `docs/schemas`.
- Regenerate schemas with `mise run schemas` whenever a model changes.
- Run `mise run check`; CI checks schema freshness and performs a strict MkDocs build.

Core document schemas remain under their document `schema` version. Runtime validation treats `$schema` only as an
untrusted editor hint and must never fetch it.
