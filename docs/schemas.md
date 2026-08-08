# JSON Schemas

Typed plugin models are the authority for runtime validation and Draft 2020-12 schema generation. Each built-in plugin
publishes four contracts:

- `unit`: authored environment input;
- `desired-unit`: the fully resolved document stored under `deploy/<environment>/units/`;
- `result`: the raw semantic result returned after applying;
- `receipt`: the generic receipt envelope composed with that plugin result.

Core schemas cover environment, promotion, materialization, desired-unit, and receipt envelopes. The complete
machine-readable catalog is [`schemas/index.json`](schemas/index.json).

## Use a pinned schema

Authored documents should point to the exact plugin version:

```json
{
  "$schema": "https://niklasrosenstein.github.io/gitopsctr/schemas/drivers/terraform/v2/unit.schema.json",
  "schema": 1,
  "name": "infrastructure",
  "driver": "terraform",
  "source": {"path": "infra", "inputs": ["*.tf"]},
  "terraform": {
    "backend": {"path": ".state/dev.tfstate"},
    "variables": {"environment": "dev"},
    "observeOutputs": []
  }
}
```

`$schema` helps editors but is never trusted by the runtime: gitopsctr does not fetch it or select validation behavior
from it. Newly generated desired units, promotions, and receipts always contain a canonical pinned URL.

`latest` aliases are convenient for discovery, but committed specifications should use pinned versions.

## CLI

```console
gitopsctr schemas show terraform receipt
gitopsctr schemas show core environment
gitopsctr schemas export docs/schemas
gitopsctr schemas export docs/schemas --check
```

`--check` fails when a current generated document is missing or stale. Historical version directories are retained and
are not removed by export.
