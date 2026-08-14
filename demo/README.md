# Demos

- [`docker`](docker/) applies a partitioned Stack that builds an OCI image and deploys it through Terraform.
- [`k8s`](k8s/) exercises dev-to-staging promotion or unpartitioned preview application, with built-in or
  Argo CD delivery to kind or minikube.

Both runners create isolated source repositories and Git remotes outside the gitopsctr repository's own refs.
