# Demos

- [`docker`](docker/) exercises a source-tracked Stack that builds an OCI image and deploys it through Terraform.
- [`k8s`](k8s/) exercises source-tracked dev-to-staging promotion or direct preview instantiation, with direct or
  Argo CD delivery to kind or minikube.

Both runners create isolated source repositories and Git remotes outside the gitopsctr repository's own refs.
