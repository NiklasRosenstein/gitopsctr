# Docker Stack demo

This demo exercises one source-tracked `Stack` with real local effects:

1. `application--image` builds the HTTP application and publishes it to a local OCI registry.
2. Its `containers` artifact carries the immutable image digest.
3. `application--deploy` resolves that artifact and uses Terraform's Docker provider to run the container.

Both Units are generated from `deployment/stack-templates/application.yaml`; there are no separately authored Units.

```console
mise install
mise run sync
mise run demo-docker run
mise run demo-docker clean
```

Override the default ports when necessary:

```console
GITOPSCTR_DEMO_REGISTRY_PORT=5001 GITOPSCTR_DEMO_APP_PORT=18081 mise run demo-docker run
```

The acceptance flow starts empty, deploys the Stack, proves a second convergence runs no drivers and moves no refs,
removes the authored Stack, and finalizes its generated Units before the Stack root:

```console
mise run demo-docker acceptance
```

The runner creates an isolated source repository and bare remote under `.docker-demo-state/`. It never writes
deployment refs to the gitopsctr repository or its remote. Acceptance and `clean` remove the container, registry,
images, Terraform state, and isolated repository.
