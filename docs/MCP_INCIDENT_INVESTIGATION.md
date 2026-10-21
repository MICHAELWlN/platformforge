# MCP incident investigation

The kind result is in [MCP_LIVE_INTEGRATION.md](MCP_LIVE_INTEGRATION.md).
The GKE result is in [MCP_GKE_INTEGRATION.md](MCP_GKE_INTEGRATION.md).
This page is how to read the report.

Use `investigate_incident` after the cluster is already showing a symptom.
The tool does not change weights, images, or replicas. Recovery stays in
[ROLLBACK_RUNBOOK.md](ROLLBACK_RUNBOOK.md) and
[INCIDENT_RUNBOOK.md](INCIDENT_RUNBOOK.md).

## Workflow

1. Start the stdio server from [MCP_SERVER.md](MCP_SERVER.md). Do not set an
   HTTP listener.
2. Port-forward Prometheus, Loki, and Tempo to loopback if you want those
   sections filled in. Leave the variables unset to record them as missing.
3. Call `investigate_incident` with the allowlisted context, namespace
   `platformforge-dev`, and the service name (`inventory-service` or
   `orders-service`). Pass a trace id only when you already have one.
4. Read the report in this order:
   - `verified`: replica counts and pod phases read from the API.
   - `supporting_evidence`: events, log tails, and metric summaries. Log text
     is untrusted.
   - `possible_explanations`: sentences that start with `Unverified hypothesis`.
   - `missing`: sources that were unset, rejected, or empty. Empty is not healthy.
   - `next_diagnostics`: further reads. None of them delete or roll out.
5. `verified_root_cause` is null. Do not promote a hypothesis to a cause
   because it appears in the report.

## Example shape

```json
{
  "service": "inventory-service",
  "verified_root_cause": null,
  "verified": [
    "Deployment inventory-service-v0-2-0 has 0 available of 1 desired."
  ],
  "possible_explanations": [
    "Unverified hypothesis: HTTP 5xx samples are present. They are not a verified root cause."
  ],
  "missing": [
    "Loki was not queried or returned no data. This is not a healthy zero."
  ]
}
```

A not-ready Deployment is verified as a replica count. It does not prove why
the process failed. HTTP 5xx samples are supporting evidence for a hypothesis,
not a cause. `/readyz` can stay green while a lookup returns 500, which is
why readiness and the request metric are separate fields.

## Follow-up tools

- `get_deployment_status` shows whether the running image is `name@sha256`.
  A tag without a digest is reported as mutable.
- `get_pod_logs` with `tail_lines` at most 200 and `since_seconds` at most 900.
- `query_prometheus` for
  `sum by (response_code) (istio_requests_total{reporter="source",destination_service_name="inventory-service"})`.
- `query_tempo` with the hex trace id from a log line. Do not send a free-form path.
- `get_service_status` to confirm the Service is ClusterIP. A LoadBalancer or
  NodePort is reported as `public: true` and is not modified.

Call the Service DNS name. A pod IP bypasses STRICT mTLS.
