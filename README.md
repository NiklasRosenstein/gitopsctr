# gitopsctr

`gitopsctr` is a local-first deployment reconciler. It turns authored environments and typed deployment units into
immutable desired-state commits, applies them through unit drivers, and records receipts and artifacts in Git.

> [!WARNING]
> gitopsctr is alpha and under active development. APIs may change before production. The bundled API kinds are the
> current built-ins, not an exhaustive set; plugins can register additional unit and artifact kinds.

## Install

```console
uv tool install gitopsctr
gitopsctr --help
```

The CLI is the same interface used locally, in CI, and by the composite GitHub Action.

## Documentation

- [Local Docker tutorial](https://niklasrosenstein.github.io/gitopsctr/tutorial/)
- [Concepts and Git state model](https://niklasrosenstein.github.io/gitopsctr/concepts/)
- [Resources and API kinds](https://niklasrosenstein.github.io/gitopsctr/documents/)
- [Built-in unit kinds](https://niklasrosenstein.github.io/gitopsctr/drivers/)
- [Reference expressions](https://niklasrosenstein.github.io/gitopsctr/references/)
- [Operations](https://niklasrosenstein.github.io/gitopsctr/operations/)
- [GitHub Action](https://niklasrosenstein.github.io/gitopsctr/github-action/)
- [JSON Schemas](https://niklasrosenstein.github.io/gitopsctr/schemas/)

The repository also contains real local [Docker](demo/docker/) and [Kubernetes](demo/kubernetes/) demonstrations.

## Development

```console
mise install
mise run sync
mise run check
```

Python 3.12 and newer are supported. See the [driver execution guide](docs/driver-execution.md) for the plugin command
boundary and the [schema guide](docs/schemas.md) for contract generation.

## License

MIT
