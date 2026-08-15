# Docker Stack demo

This demo applies one authored `Stack` as the authoritative `application` partition and exercises real local effects:

1. `application--image` builds the HTTP application and publishes it to a local OCI registry.
2. Its `containers` artifact carries the immutable image digest.
3. `application--deploy` resolves that artifact and uses Terraform's Docker provider to run the container.

Both Units are generated from the inline `StackTemplate` in `deployment/stack-templates/application.yaml`; there are no
separately authored Units. The demo passes that template explicitly with the Stack, so its desired acquisition mode is
`input`; the desired record's `documentDigest` identifies the serialized input bytes and `contentDigest` identifies the
semantic template content.

```console
mise install
mise run sync
mise run demo-docker run
mise run demo-docker clean
```

Inspect the template root and its generated Stack while the demo repository is available:

```console
gitopsctr get stacktemplates --environment dev
gitopsctr get stack application --environment dev
```

Override the default ports when necessary:

```console
GITOPSCTR_DEMO_REGISTRY_PORT=5001 GITOPSCTR_DEMO_APP_PORT=18081 mise run demo-docker run
```

The acceptance flow starts empty, applies and deploys the Stack, proves a second application and convergence are
no-ops, removes the Stack from the explicitly applied partition, and lets convergence tear down its generated Units
child-first before removing the Stack root:

```console
mise run demo-docker acceptance
```

The runner creates an isolated source repository and bare remote under `.docker-demo-state/`. It never writes
deployment refs to the gitopsctr repository or its remote. Acceptance and `clean` remove the container, registry,
images, Terraform state, and isolated repository.
