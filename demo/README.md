# Demos

- [`docker`](docker/) builds an OCI image, publishes it to a local registry, and deploys it with Terraform and Docker.
- [`kubernetes`](kubernetes/) builds an OCI image, loads it directly into an explicitly selected kind or minikube
  cluster, renders a Helm chart, and deploys and verifies the application.

Both runners create isolated source repositories and Git remotes outside the gitopsctr repository's own refs.
