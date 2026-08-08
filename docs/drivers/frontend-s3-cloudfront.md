# Frontend S3/CloudFront unit driver

The Frontend S3/CloudFront driver consumes an immutable OCI bundle, extracts
it, uploads the files to S3, and invalidates CloudFront. It also writes a
runtime configuration document so the deployed frontend can discover its API
and authentication settings.

**Kind:** `unit.gitopsctr.io/v1/FrontendS3Cloudfront`<br>
**Version:** `v1`<br>
**Capabilities:** planning, reconciliation

## Authored unit

```yaml
$schema: https://niklasrosenstein.github.io/gitopsctr/schemas/apis/unit.gitopsctr.io/v1/FrontendS3Cloudfront/authored.schema.json
apiVersion: unit.gitopsctr.io/v1
kind: FrontendS3Cloudfront
metadata:
  name: frontend
spec:
  source:
    path: web
  inputs:
    bundle: registry.example/frontend@sha256:0000000000000000000000000000000000000000000000000000000000000000
    bucket: example-frontend
    distributionId: EXAMPLE123
    url: https://www.example.invalid
    runtimeConfig:
      schema: 1
      apiBase: https://api.example.invalid
      auth:
        mode: cognito
        issuer: https://issuer.example.invalid
        clientId: example-client
  pull:
    credentialProvider:
      type: aws-ecr
```

`inputs.bundle` must be an immutable OCI digest URI. In promotion-tracked
environments it is commonly a `fromPromotion` reference to the bundle unit.
`bucket`, `distributionId`, and `url` select the publication target. The
runtime configuration is an exact schema-1 object and currently uses Cognito
authentication. `pull.credentialProvider` supports AWS ECR.

Planning downloads and validates the bundle without publishing it.
Reconciliation syncs the extracted files, uploads `runtime-config.json`, and
invalidates CloudFront before returning the published URL and digests.

## Schemas

- [authored unit](../schemas/drivers/frontend-s3-cloudfront/v1/unit.schema.json)
- [desired unit](../schemas/drivers/frontend-s3-cloudfront/v1/desired-unit.schema.json)
- [result](../schemas/drivers/frontend-s3-cloudfront/v1/result.schema.json)
- [receipt](../schemas/drivers/frontend-s3-cloudfront/v1/receipt.schema.json)
