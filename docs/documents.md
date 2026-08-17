# Resources and API kinds

gitopsctr accepts YAML and JSON resources identified by `apiVersion` and `kind`. YAML is preferred for authored and
generated state; the repository `Project` can select JSON instead. Runtime validation uses the registered typed
contract and never fetches the document's `$schema` editor hint.

!!! warning "Alpha API"

    gitopsctr is under active development. APIs may change before the project reaches production. The kinds below are
    the current built-ins, not a complete ecosystem: plugins can register additional unit and artifact kinds by
    full group/version/kind.

## Repository layout

Every source tree contains `gitopsctr.yaml` and one authored `Environment` directory per environment:

```text
gitopsctr.yaml
deployment/environments/
└── dev/
    ├── environment.yaml
    └── units/
        └── application.yaml
```

`spec.environmentsPath` may change the authored environment directory. An Environment is the namespace boundary for
environment-scoped resources. Generated desired and observed refs use logical collections registered for their
resource families; for example, desired Units and observed Receipts use `units/<qualified-unit>/`, artifacts use
`artifacts/<qualified-unit>/`, and materialized payloads use `materialized/<qualified-unit>/`.

Create and validate authored resources with:

```console
gitopsctr create project --name example
gitopsctr create environment --name dev
gitopsctr create unit --environment dev --name infrastructure --driver terraform --source-path infrastructure
gitopsctr validate
```

Creation follows the Project's configured document format and never replaces an existing resource unless `--force` is
explicit. See [Project configuration](project-configuration.md) and [Operations](operations.md).

## Resource definitions and representations

The typed resource registry is the semantic catalog for gitopsctr's API kinds. A resource family defines its
singular and plural selectors, and each placement defines one allowed representation in the source, desired, or
observed plane. A family can have multiple representations: an authored Unit and its resolved desired Unit are the
same API family in different planes, not unrelated kinds.

A family's local identity, placement, and address rule belong to the registry. Its collection adapter encodes the
resulting `qualifiedName` into a canonical path while the document retains its local `metadata.name`. Root, child, and
mirror rules compose hierarchy from validated relationships without teaching the CLI a fixed number of identity
levels. Installed plugins can add a
`ResourceModelContribution` through the `gitopsctr.resource-models` entry-point group; contributions may contain
collections, families, observations, artifact descriptions, graph relationships, presenters, and address rules.
Selectors and filter options are derived only after the combined registry validates successfully.

Relationship definitions connect otherwise independent resources. In particular, a Receipt observes a desired Unit
by subject identity and desired-unit blob, while its artifact descriptors refer to separately stored Artifact
resources. Those definitions are invariants; each YAML or JSON document is a concrete instance. See the generated
[resource model](resource-model.md) for the complete built-in placement matrix and relationships.

## Controller resources

Controller resources use `gitopsctr.io/v1`.

| Kind | Purpose and ownership | Location |
| --- | --- | --- |
| [Project](project-configuration.md) | User-authored repository identity, document format, environment path, and ref defaults | `gitopsctr.yaml` |
| [Environment](apis/environment.md) | User-authored refs, change gate, promotion sources, and evidence policy | `<environmentsPath>/<name>/environment.*` |
| [StackTemplate](apis/stacks.md) | User-authored, parameterized collection of Unit templates | `<stackTemplatesPath>/<name>.*`; resolved copy on the desired ref |
| [Stack](apis/stacks.md) | Authored or canonical desired instance of a StackTemplate | `<environment>/stacks/<name>.*`; resolved copy on the desired ref |
| [Promotion](apis/promotion.md) | Controller-owned lineage pinning source desired, observed, and specification revisions | `promotion.*` on the target desired ref |
| [Receipt](apis/receipt.md) | Separate controller- and driver-owned observation of one exact desired Unit | `units/<qualified-unit>.*` on the observed ref |

## Unit resources

Unit resources use `unit.gitopsctr.io/v1`. A unit is authored by the user, resolved into desired state by the
controller, and implemented by its registered unit driver.

| Kind | Purpose and ownership | Location | Guide |
| --- | --- | --- | --- |
| `Terraform` | User-authored Terraform deployment; the driver plans, applies, and verifies it | Authored below `<environment>/units/`; resolved at `units/<qualified-unit>.*` | [Terraform](drivers/terraform.md) |
| `OciImages` | User-authored image build; the driver publishes immutable OCI images | Authored below `<environment>/units/`; resolved at `units/<qualified-unit>.*` | [OCI images](drivers/oci-images.md) |
| `ViteOciBundle` | User-authored frontend build; the driver publishes an OCI bundle | Authored below `<environment>/units/`; resolved at `units/<qualified-unit>.*` | [Vite OCI bundle](drivers/vite-oci-bundle.md) |
| `FrontendS3Cloudfront` | User-authored publication; the driver deploys a bundle to S3 and CloudFront | Authored below `<environment>/units/`; resolved at `units/<qualified-unit>.*` | [Frontend S3/CloudFront](drivers/frontend-s3-cloudfront.md) |
| `KubernetesManifests` | User-authored delivery; the driver renders and optionally applies Kubernetes resources | Authored below `<environment>/units/`; resolved at `units/<qualified-unit>.*` | [Kubernetes manifests](drivers/kubernetes-manifests.md) |

Each kind publishes `authored`, `desired`, and `receipt` contract profiles. The authored and desired profiles are Unit
representations. The receipt profile specializes the separate Receipt resource for that subject kind; it is not a
third Unit representation or embedded Unit status. Use the authored schema in source repositories; desired Units and
Receipts are controller-owned. The [unit kind overview](drivers.md) compares their capabilities.

## Artifact resources

Artifact resources use `artifact.gitopsctr.io/v1` and are published by drivers on the observed ref. Their typed
contracts are independent of the unit kinds that produce them.

| Kind | Purpose and ownership | Location |
| --- | --- | --- |
| [ContainerImages](apis/container-images.md) | Driver-owned immutable image names, tags, and digests produced by `OciImages` | `artifacts/<qualified-unit>/<name>.*` on the observed ref |
| [FrontendBundle](apis/frontend-bundle.md) | Driver-owned immutable bundle URI and metadata produced by `ViteOciBundle` | `artifacts/<qualified-unit>/<name>.*` on the observed ref |

Receipts describe every artifact's GVK, path, media type, and serialized-byte digest. The
[artifact overview](apis/artifacts.md) explains how consumers find and validate these resources through
[`fromArtifact`](references.md).

## Schemas and extensibility

The [schema catalog](schemas.md) is the complete structural reference for the built-ins. Extensible API kinds are
discovered through full-GVK `gitopsctr.apis` Python entry points. Unit API registrations provide a `UnitDriver`;
artifact API registrations provide typed parsing, media type, and schema generation. Driver capabilities such as
planning, materialization, reconciliation, and verification remain independent.
