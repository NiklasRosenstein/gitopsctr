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

`spec.environmentsPath` may change the authored environment directory. Generated desired and observed refs always use
top-level `units/`, with artifacts under `artifacts/<unit>/` and materialized payloads under `materialized/<unit>/`.

Create and validate authored resources with:

```console
gitopsctr create project --name example
gitopsctr create environment --name dev
gitopsctr create unit --environment dev --name infrastructure --driver terraform --source-path infrastructure
gitopsctr validate
```

Creation follows the Project's configured document format and never replaces an existing resource unless `--force` is
explicit. See [Project configuration](project-configuration.md) and [Operations](operations.md).

## Controller resources

Controller resources use `gitopsctr.io/v1`.

| Kind | Purpose and ownership | Location |
| --- | --- | --- |
| [Project](project-configuration.md) | User-authored repository identity, document format, environment path, and ref defaults | `gitopsctr.yaml` |
| [Environment](apis/environment.md) | User-authored refs, change gate, promotion sources, and evidence policy | `<environmentsPath>/<name>/environment.*` |
| [Promotion](apis/promotion.md) | Controller-owned lineage pinning source desired, observed, and specification revisions | `promotion.*` on the target desired ref |
| [Receipt](apis/receipt.md) | Controller- and driver-owned evidence for one exact desired unit | `units/<name>.*` on the observed ref |

## Unit resources

Unit resources use `unit.gitopsctr.io/v1`. A unit is authored by the user, resolved into desired state by the
controller, and implemented by its registered unit driver.

| Kind | Purpose and ownership | Location | Guide |
| --- | --- | --- | --- |
| `Terraform` | User-authored Terraform deployment; the driver plans, applies, and verifies it | Authored below `<environment>/units/`; resolved at `units/<name>.*` | [Terraform](drivers/terraform.md) |
| `OciImages` | User-authored image build; the driver publishes immutable OCI images | Authored below `<environment>/units/`; resolved at `units/<name>.*` | [OCI images](drivers/oci-images.md) |
| `ViteOciBundle` | User-authored frontend build; the driver publishes an OCI bundle | Authored below `<environment>/units/`; resolved at `units/<name>.*` | [Vite OCI bundle](drivers/vite-oci-bundle.md) |
| `FrontendS3Cloudfront` | User-authored publication; the driver deploys a bundle to S3 and CloudFront | Authored below `<environment>/units/`; resolved at `units/<name>.*` | [Frontend S3/CloudFront](drivers/frontend-s3-cloudfront.md) |
| `KubernetesManifests` | User-authored delivery; the driver renders and optionally applies Kubernetes resources | Authored below `<environment>/units/`; resolved at `units/<name>.*` | [Kubernetes manifests](drivers/kubernetes-manifests.md) |

Each kind publishes `authored`, `desired`, and `receipt` schema profiles. These are lifecycle views of the unit and its
generic receipt, not separate unit GVKs. Use the authored schema in source repositories; desired units and receipts are
controller-owned. The [unit kind overview](drivers.md) compares their capabilities.

## Artifact resources

Artifact resources use `artifact.gitopsctr.io/v1` and are published by drivers on the observed ref. Their typed
contracts are independent of the unit kinds that produce them.

| Kind | Purpose and ownership | Location |
| --- | --- | --- |
| [ContainerImages](apis/container-images.md) | Driver-owned immutable image names, tags, and digests produced by `OciImages` | `artifacts/<unit>/<name>.*` on the observed ref |
| [FrontendBundle](apis/frontend-bundle.md) | Driver-owned immutable bundle URI and metadata produced by `ViteOciBundle` | `artifacts/<unit>/<name>.*` on the observed ref |

Receipts describe every artifact's GVK, path, media type, and serialized-byte digest. The
[artifact overview](apis/artifacts.md) explains how consumers find and validate these resources through
[`fromArtifact`](references.md).

## Schemas and extensibility

The [schema catalog](schemas.md) is the complete structural reference for the built-ins. Extensible API kinds are
discovered through full-GVK `gitopsctr.apis` Python entry points. Unit API registrations provide a `UnitDriver`;
artifact API registrations provide typed parsing, media type, and schema generation. Driver capabilities such as
planning, materialization, reconciliation, and verification remain independent.
