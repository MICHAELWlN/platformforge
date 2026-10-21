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

Checked on 2026-10-21. `gh` is logged in as `MICHAELWlN`. There is no git
remote. `MICHAELWlN/platformforge` does not exist, and that account has no
repository named platformforge. Nothing has been pushed.

Prepared creation, not executed:

1. Create a private repository `MICHAELWlN/platformforge` and push `main`
   only after a person approves the publish. The secret scan must be clean
   first. Terraform state, `terraform.tfvars`, and `deploy/gke/image-lock.yaml`
   are gitignored and are not in the index.
2. Set `github_repository` to `MICHAELWlN/platformforge` only in an untracked
   tfvars file. Leave `enable_github_delivery` false until a separate apply
   is approved.
3. Create the GitHub environment `gke-delivery` before any delivery variable
   is set. Required settings:
   - at least one required reviewer
   - deployment branches limited to `main`
   - no wait-timer-only substitute for a reviewer
4. Read the environment back with the GitHub API and confirm reviewers and
   the `main` branch rule are present. GitHub creates an environment with no
   reviewers the first time a job names it, so this check has to happen first.
5. A solo owner who is the only required reviewer cannot approve their own
   run when self-review is prevented. Add a second reviewer in that case.
   Do not turn self-review prevention off to make a publish succeed.
6. After the apply, set repository variables, not secrets:
   - `GCP_WORKLOAD_IDENTITY_PROVIDER` from the Terraform output
   - `GCP_DELIVERY_SERVICE_ACCOUNT` =
     `platformforge-dev-cd@platformforge-dev-mn2026.iam.gserviceaccount.com`
   - `GCP_DELIVERY_ENABLED` = `true` only after steps 3 and 4 pass

`GCP_DELIVERY_ENABLED` stays unset. The workflow has not been dispatched.

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

Create that environment with required reviewers before setting the variable.
GitHub otherwise creates an environment with no reviewers on first use.

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
resources have no meaningful hourly charge. The cluster's existing cost is
unchanged until someone applies `enable_github_delivery = true`.
