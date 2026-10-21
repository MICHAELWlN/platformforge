# Continuous integration and delivery

Milestone 6 adds a credential-free CI workflow and a manual delivery workflow.
CI does not receive cloud credentials. Delivery does not run on pull requests,
and it does not change the GKE cluster.

## Pipelines

```
pull request or push to main
  lint (ruff)
  unit tests, excluding kind and GKE
  terraform fmt / init -backend=false / validate
  ServiceManifest validation and public-exposure scan
  secret scan of files Git would publish
  pip-audit of services/requirements.txt
  linux/amd64 image build, non-root smoke test, Trivy CRITICAL scan
        |
        | no registry push, no kubectl
        v
workflow_dispatch on main, only if var GCP_DELIVERY_ENABLED is true
  publish job: GitHub OIDC, build amd64, scan, push digest
  gate job: check project, cluster, and context, then refuse apply
```

Workflow files:

| File | Trigger | Cloud token |
| --- | --- | --- |
| `.github/workflows/ci.yml` | `push` to `main`, `pull_request` | none |
| `.github/workflows/delivery.yml` | `workflow_dispatch` only | `id-token: write` on the publish job |

Both pin third-party actions to commit SHAs. Top-level `permissions` is
`contents: read`. CI concurrency cancels older runs on the same ref. Delivery
concurrency is `platformforge-gke-delivery` and does not cancel an in-progress
run.

Pull requests, including forks, never match the delivery trigger, so they
cannot receive the OIDC token.

## Authentication

GitHub Actions authentication is Workload Identity Federation, prepared in
`infra/terraform/modules/github_delivery/` and wired from
`infra/terraform/github_delivery.tf`. `enable_github_delivery` defaults to
`false`, so a normal apply does not create the pool. There is no
service-account JSON key and no `GOOGLE_APPLICATION_CREDENTIALS` secret.

When enabled, the provider accepts tokens only when all of these hold:

- `assertion.repository` equals `github_repository` (`owner/name`)
- `assertion.ref` is `refs/heads/main`
- `assertion.environment` is `gke-delivery`
- issuer is `https://token.actions.githubusercontent.com`

The delivery service account is `platformforge-dev-cd`. It can upload image
versions to the existing `platformforge` repository and cannot delete them.
It can call `container.clusters.get` only when the resource type is
`container.googleapis.com/Cluster` and the resource name is
`projects/platformforge-dev-mn2026/zones/us-central1-a/clusters/platformforge-dev`.
That is the zonal form. The live cluster selfLink uses the same project id,
`zones/us-central1-a`, and cluster name. A regional cluster would use
`locations` instead. The binding is not a wildcard principal. Branch and
environment are enforced by the provider `attribute_condition`, which runs
before the token can impersonate the account.

Kubernetes object access is the Role in `deploy/gke/delivery-rbac.yaml`, which
is not applied. That Role has no delete verb and cannot change Service type.

## GitHub repository

Checked on 2026-10-21. `origin` is
`https://github.com/MICHAELWlN/platformforge.git`. `main` matches
`origin/main` at `bcb2bb6`. The repository is public. No GitHub Actions
secrets or variables are set. Terraform state, `terraform.tfvars`, and
`deploy/gke/image-lock.yaml` are gitignored and are not in the index.

Environment `gke-delivery` was created and read back:

- Required reviewer: `MICHAELWlN`.
- Deployment branches: custom policy, only `main`.
- `can_admins_bypass` is false, so an admin cannot skip the review.
- `prevent_self_review` is false. The only GitHub user available is the
  repository owner, who is also the reviewer. Turning self-review prevention
  on would make an approval impossible. It was not turned off from an
  existing true value. A second reviewer is the missing control.
- `main` itself has no branch protection. The environment rule limits which
  ref may use the environment. It does not stop the owner from pushing to
  `main`.

The committed default of `enable_github_delivery` is still false. The local
gitignored `terraform.tfvars` enables the module. The 11 delivery resources
are in the Terraform state. This pass did not apply or destroy them.

Repository variables, not secrets:

- `GCP_WORKLOAD_IDENTITY_PROVIDER` =
  `projects/345366894072/locations/global/workloadIdentityPools/platformforge-github/providers/github-actions`
- `GCP_DELIVERY_SERVICE_ACCOUNT` =
  `platformforge-dev-cd@platformforge-dev-mn2026.iam.gserviceaccount.com`
- `GCP_DELIVERY_ENABLED` = `true`

Those identity values match `terraform output`. `GCP_DELIVERY_ENABLED` was
set only after the pool, provider, service account, and environment were
read back. No GitHub Actions secret stores a GCP key.

## Publishing

`make images-gke` still builds locally and does not push. The delivery
workflow builds `linux/amd64` for version `0.1.0` or `0.2.0`, checks the
architecture, runs Trivy at CRITICAL with unfixed CVEs ignored, then pushes
the version tag. The digest is printed from Artifact Registry. Deployments
must use `name@sha256`, which `platformforge.delivery.require_deployable_image`
enforces. `:latest` is refused. Authentication is `gcloud auth configure-docker`
from the OIDC token. There is no JSON key.

The publish job runs only when all of these are true:

- event is `workflow_dispatch`
- ref is `refs/heads/main`
- input `publish` is true
- repository variable `GCP_DELIVERY_ENABLED` is `true`
- environment `gke-delivery` allows the job

The `gke-delivery` environment already requires a review. A dispatch from
`main` waits for `MICHAELWlN` to approve it. Other branches cannot use the
environment.

## Terraform plan

`terraform plan` on 2026-10-21, before the apply, reported **11 to add, 0 to
change, 0 to destroy**. Nothing in the GKE cluster, VPC, NAT, or node IAM was
in that plan. The apply was completed outside this pass. A later plan reported
no changes. This pass did not run `terraform apply` or `terraform destroy`.

The 11 creates are the GitHub pool `platformforge-github`, provider
`github-actions`, service account `platformforge-dev-cd`, the
`roles/iam.workloadIdentityUser` binding for `MICHAELWlN/platformforge`,
custom roles `platformforgeDeliveryPublisher` and
`platformforgeDeliveryClusterGet`, the repository IAM member, the conditional
project IAM member, and API entries for Artifact Registry, IAM Credentials,
and Security Token Service. `disable_on_destroy` is false on those APIs, so
a later destroy does not disable them. No service-account key is in the plan.

Run
[39546044442](https://github.com/MICHAELWlN/platformforge/actions/runs/39546044442)
completed with conclusion `success` on `55f7f4c`. It published `0.2.0` and
did not change the cluster. The published digests are not the digests the
running Deployments use.

```
inventory-service@sha256:52c704a597302abbf25e19cb011592e9df22941ffc3a9ba949a58b7cc33a45bf
orders-service@sha256:038cdce9303a95716b94a7759a107eae20b336954f343aa9cb61abbce6dc8b06
```

Both registry config blobs are `linux/amd64` and user `65532:65532`.

Authentication flow for that run:

1. A person dispatches `.github/workflows/delivery.yml` from `main`.
2. GitHub holds the job until the `gke-delivery` reviewer approves it.
3. GitHub issues an OIDC token whose `repository`, `ref`, and `environment`
   claims must match the provider condition.
4. Google exchanges that token for `platformforge-dev-cd`.
5. The job pushes a digest. `refuse-apply` still exits 2. The workflow
   requires that exit code and does not run kubectl.

## Failure recovery

A failed publish leaves the previous digest in place. The workflow does not
delete image versions, and the publisher role cannot delete them. Re-run the
same dispatch after the scan or the build is fixed. The delivery lock and
canary rollback in [ROLLBACK_RUNBOOK.md](ROLLBACK_RUNBOOK.md) apply only after
a later, separately approved apply path exists. `refuse-apply` still exits 2,
so a failed gate does not change the cluster.

If a token were ever issued for the wrong repository, delete the workload
identity provider before changing its condition. Do not create a key to
"debug" authentication.

## What this does not do

The gate job ends in `python -m platformforge.delivery refuse-apply`. It does
not run `kubectl apply`. `platformforge deploy` still refuses any context that
does not start with `kind-`. Live promotion is specified in
[PROGRESSIVE_DELIVERY.md](PROGRESSIVE_DELIVERY.md) and is not executed by the
workflow.

## Cost

CI uses GitHub-hosted minutes only. A publish, once approved, stores more
bytes in the existing Artifact Registry repository and uses Cloud NAT to pull
on the node. It does not add a node, disk, or load balancer. Federation
resources have no meaningful hourly charge. The cluster's existing cost is unchanged. The plan does not add a node,
disk, or load balancer. Workload Identity Federation and custom roles have
no meaningful hourly charge. Enabling the Security Token Service and IAM
Credentials APIs does not add a cluster charge. A later image push stores
more bytes in the existing Artifact Registry repository.
