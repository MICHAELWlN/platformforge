# Cloud costs

Nothing in this estimate was provisioned. There is no confirmed charge.
The figures below are published list prices, read on 2026-10-20, multiplied
by 730 hours (24 × 365 / 12). A negotiated contract, a tax, or a later price
change is not included. E2 VMs have no automatic sustained-use discount. No
committed-use discount is assumed.

The Cloud Billing Catalog was not queried. An earlier check that day had no
credentialed account. A later readiness pass had an account and did not
re-query the catalog. Billing on `platformforge-dev-mn2026` was verified
enabled. The prices below are still list prices, not an invoice.

The configuration priced is the Terraform default: one zonal Standard cluster
in `us-central1-a`, one on-demand `e2-standard-4`, a 50 GB `pd-balanced` boot
disk, Cloud NAT with one address, system-only Cloud Logging and Monitoring,
and no load balancer. Other regions have different list prices. This page
does not price them.

## Sources

| Item | Source read on 2026-10-20 |
| --- | --- |
| Cluster management fee $0.10/cluster/hour, free-tier credit $74.40 | [GKE pricing](https://cloud.google.com/kubernetes-engine/pricing) |
| `e2-standard-4` in us-central1, $0.134032/hour on demand | Published machine-type SKU table for Iowa. [Economize](https://www.economize.cloud/resources/gcp/pricing/compute-engine/e2-standard-4/), updated 2026-10-19, lists $0.1340/hour and $97.84/month at 730 hours |
| Spot snapshot $0.0804/hour, $58.71/month | Same Economize page. Spot prices can change and are not a quote |
| Balanced PD in Iowa, $0.000136986 per GiB-hour | [Disk pricing](https://cloud.google.com/compute/disks-image-pricing) |
| Cloud NAT: $0.0014 × VM-hours up to 32 VMs, $0.045/GiB processed, $0.005/hour per NAT address | [Cloud NAT](https://cloud.google.com/nat) |
| Premium Tier internet egress from Iowa to North America: first 1 GiB free, then $0.12/GiB | [Network pricing](https://cloud.google.com/vpc/network-pricing) |
| Logging: first 50 GiB/project/month free, then $0.50/GiB. Vended network logs $0.25/GiB, no free allotment | [Observability pricing](https://cloud.google.com/products/observability/pricing) |
| Monitoring: first 150 MiB/billing account free for chargeable metrics | Same observability page |

## Fixed monthly estimate, one node

These lines do not depend on how much traffic the lab sends.

| Line | Formula | Amount |
| --- | --- | --- |
| `e2-standard-4` on demand | 0.134032 × 730 | $97.84 |
| 50 GB pd-balanced boot disk | 0.000136986 × 50 × 730 | $5.00 |
| Cloud NAT gateway, 1 VM | 0.0014 × 1 × 730 | $1.02 |
| One NAT external address | 0.005 × 730 | $3.65 |
| **Fixed subtotal** | | **$107.51** |

GKE cluster management accrues $0.10 × 730 = $73.00. The free-tier credit is
$74.40 per billing account per month and applies only to zonal and Autopilot
cluster fees. It does not pay for VMs, disks, or NAT.

| Situation | Billable management fee | Fixed total |
| --- | --- | --- |
| This is the billing account's only zonal or Autopilot cluster | $0.00 | $107.51 |
| Another cluster already consumes the credit | $73.00 | $180.51 |

Which row applies was checked later on 2026-10-20, without creating
resources. The open billing account has two projects:
`platformforge-dev-mn2026` and `project-60f4c264-06bf-4137-83e`. Kubernetes
Engine is disabled on both, and neither returned a cluster list. There is
no positive evidence of another zonal or Autopilot cluster, so the
free-tier row ($107.51 fixed) is the expected case. That is an inference
from API status, not from a successful `clusters list`. A cluster created
later on this billing account would move the bill to the $180.51 row.

## Example usage on top of the fixed subtotal

This is an illustration of 5 GiB of internet image pulls in a month, Premium
Tier, from `us-central1` to North America. It is not a forecast and it was
not measured.

| Line | Formula | Amount |
| --- | --- | --- |
| Internet egress | 1 GiB free + 4 × $0.12 | $0.48 |
| NAT data processing | 5 × $0.045 | $0.23 |
| **Example usage** | | **$0.71** |

A quiet month with the free tier available is then about **$108.22**. The
same example without the free tier is about **$181.22**. Pulling the Istio
and observability images once is on the order of a few GiB, not tens. A loop
that pulls large images, or any workload that talks to the public internet,
moves this line. Private Google Access keeps calls to Google APIs off Cloud
NAT; those calls are not in the 5 GiB example.

## Two-node ceiling

The pool's default maximum is 2. A node upgrade also uses a second VM for the
length of the upgrade (`max_surge = 1`), even when the steady state is 1.
Thirty minutes of one extra `e2-standard-4` is about 0.134032 × 0.5 = $0.07.
That is not part of the monthly fixed total.

If the autoscaler sits at 2 nodes for the whole month, and NAT still uses one
address:

| Line | Amount |
| --- | --- |
| Two VMs | $195.68 |
| Two boot disks | $10.00 |
| NAT gateway, 2 VMs | $2.04 |
| One NAT address | $3.65 |
| **Fixed subtotal** | **$211.37** |
| Same 5 GiB example | $0.71 |
| Example month, free tier available | $212.08 |
| Example month, free tier already used | $285.08 |

NAT may allocate more addresses if port usage requires it. Each extra address
is another $3.65 at this list price.

## What is priced at zero because it is not created

| Item | Why it is $0 in this configuration |
| --- | --- |
| Load balancers | No forwarding rule, Ingress, or Gateway. The HTTP load-balancing add-on is disabled. A later `Service` of type `LoadBalancer` would add a forwarding-rule SKU; this root does not create one |
| Extra persistent disks | Boot disks only. The platform's metrics, logs, and traces use emptyDir |
| Artifact Registry, KMS, Binary Authorization, Filestore, Backup for GKE | Not in the configuration |
| Managed Service for Prometheus | `managed_prometheus.enabled = false` |
| VPC flow logs | Off. They are vended logs at $0.25/GiB with no free allotment, plus generation charges. Checkov CKV_GCP_26 reports the gap |
| Workload Cloud Logging | `logging_config` is `SYSTEM_COMPONENTS` only. Application logs stay in the cluster (Loki) after that stack is installed by Helm |

Cloud Logging's 50 GiB free allotment and Monitoring's 150 MiB free allotment
are expected to cover a one-node cluster that exports system components only.
That is an expectation, not a measurement. Past the allotment, logs are
$0.50/GiB and chargeable metrics follow the observability price card. NAT
error logs are vended and have no free allotment. `ERRORS_ONLY` should stay
small unless NAT is failing. If it does not, they bill at $0.25/GiB.

## Spot

`use_spot_nodes` defaults to false. The Economize snapshot on 2026-10-19 was
$58.71/month ($0.0804/hour) for one Spot `e2-standard-4` in `us-central1`,
about $39 less than on demand. Replacing only the VM line in the one-node
fixed subtotal gives $68.38 before the free-tier question and before usage.
Spot can be preempted with little notice and the price can change. A
preemption of the only node takes the mesh and the telemetry stack with it.

## What would change the bill without a Terraform edit

- A second zonal or Autopilot cluster on the same billing account, which
  consumes the free-tier credit.
- A regional cluster. The same $0.10/hour accrues, and the free-tier credit
  does not apply to regional clusters. The default node placement would also
  be three zones.
- Setting `release_channel` to `EXTENDED` while the minor is in extended
  support. Google charges an additional management fee for that case. The
  default channel is `REGULAR`.
- Leaving the cluster running. There is no schedule that scales to zero.
- `deletion_protection` does not affect the bill. It only blocks Terraform
  from deleting the cluster until it is set to false and applied.

## Budget alert

A $25 monthly alert was described for this project. The Cloud Billing Budget
API (`billingbudgets.googleapis.com`) is disabled on
`platformforge-dev-mn2026`, and the readiness pass did not enable it, so the
budget object was not re-read. Treat $25 as a stated alert threshold.

A budget alert does not stop spend and does not change this configuration.
The one-node fixed estimate with the free tier available is $107.51 before
any image pull or other egress. The alert should be expected to fire if the
cluster is left running. It is not a spending cap.

Charges this root can add, beyond the fixed lines above:

| Source | What this configuration does |
| --- | --- |
| GKE management | $0.10/cluster/hour, expected to be covered by the free-tier credit while no other zonal or Autopilot cluster shares the billing account. `EXTENDED` is not the default, so the extended-support surcharge does not apply |
| Compute Engine | One on-demand `e2-standard-4` at steady state. Create also brings up a default node pool of one VM and then deletes it. An upgrade can add a second `e2-standard-4` for the length of the surge |
| Persistent disk | One 50 GB `pd-balanced` boot disk per node. No extra disk is declared. The temporary default-pool VM has its own boot disk for the length of create |
| Cloud NAT | Gateway fee per VM-hour, one address at $0.005/hour, and $0.045/GiB processed. `AUTO_ONLY` can allocate more addresses if ports run out. Each extra address is another $3.65 at this list price |
| Network egress | Premium Tier internet egress after the first 1 GiB. Private Google Access keeps Google API calls off NAT. Pods can still reach the public internet |
| Artifact Registry | Not created. Storage and egress for a later registry are not in this estimate |
| Logging and monitoring | System components only, expected to stay inside the 50 GiB and 150 MiB free allotments. That is not a measurement. NAT error logs are vended at $0.25/GiB with no free allotment. Workload logs stay in the cluster after Loki is installed. Managed Prometheus is off. Flow logs are off |
