# Available unit drivers

A **unit driver** implements one unit kind. A unit is the named instance in an
environment DAG; the driver supplies the behavior for that instance. Drivers
can expose independent capabilities:

- **planning** previews external work without changing external state;
- **materialization** produces files in the desired tree;
- **reconciliation** applies the desired state and publishes a receipt;
- **verification** checks external state without writing a receipt.

Every driver is registered by its full kind (`unit.gitopsctr.io/v1/<Kind>`) and
publishes resource schemas for its authored unit, desired unit, and receipt.
Use the pinned authored schema in source repositories and keep the generated
desired and receipt documents under controller ownership.

## Built-in drivers

| Driver | Kind | Capabilities | Typical effect |
| --- | --- | --- | --- |
| [Terraform](drivers/terraform.md) | `Terraform` | plan, reconcile, verify | plans and applies Terraform configurations |
| [OCI images](drivers/oci-images.md) | `OciImages` | plan, reconcile | builds and publishes container images |
| [Vite OCI bundle](drivers/vite-oci-bundle.md) | `ViteOciBundle` | plan, reconcile | builds a Vite site and publishes an OCI bundle |
| [Frontend S3/CloudFront](drivers/frontend-s3-cloudfront.md) | `FrontendS3Cloudfront` | plan, reconcile | pulls a bundle and publishes it to S3/CloudFront |
| [Kubernetes manifests](drivers/kubernetes-manifests.md) | `KubernetesManifests` | materialize, plan, reconcile, verify | renders manifests and optionally delivers them |
