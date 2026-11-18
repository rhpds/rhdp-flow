# Parent–Child deployment contract

The `parent_child_v1` implementation is disabled by default. No deployment or
live resource orders are part of this change. Ordinary CSV keeps its existing
semantics and rejects dependency markers instead of flattening their graph.

The API and worker use the same immutable plan from staging through deployment.
The review screen is `/?dependency_plan=<plan-id>&target_cluster=<target>#dependencies`. It displays
catalog identities, quantities, schedules, and binding sources, masks ordinary
parameter values, and starts the staged ID with that ID as its idempotency key.
The plan never includes resolved parent credentials. API-key authentication and
the existing deploy-target operator policy protect reads and mutations.

## Runtime requirements

Future enablement requires `RHDP_ENABLE_DEPENDENCIES=true`,
`RHDP_DEPENDENCY_INSTANCE_COUNT=1`, `WEB_CONCURRENCY=1`, a persistent
`RHDP_FLOW_DATA_DIR`, and the actual Poolboy operator namespace in
`RHDP_DEPENDENCY_POOLBOY_NAMESPACE`. SQLite plus an exclusive process-held file
lock supports a single service instance. Multiple replicas and independently
mounted copies of the state directory are unsupported. Deployment manifests
are deliberately unchanged; these settings are not enabled by this branch.

Plans stage only for an explicit, immutable target selected by the existing
`X-RHDP-Target-Cluster` header (or query argument). `cluster_id` remains the
Labagator numeric identity; it is never guessed to be a Flow target Secret key.
Namespace SelfSubjectAccessReviews check the target service account's actual
permissions. Every subsequent operation rechecks stored-target authorization
and namespace permissions and resolves ephemeral credentials for that target.

## HTTP transport

All paths also have the existing `/api/v1` alias. Send `X-API-Key` as usual.

| Operation | Request |
| --- | --- |
| Capability | `GET /api/capabilities` |
| Immutable stage | `POST /api/dependency-plans`, raw versioned plan JSON |
| Review | `GET /api/dependency-plans/{plan_id}` |
| Start | `POST /api/dependency-plans/{plan_id}/runs`, `Idempotency-Key: {plan_id}` |
| Safe status | `GET /api/dependency-runs/{run_id}` |
| Cancel future submissions | `POST /api/dependency-runs/{run_id}/cancel` |
| Retry named failed children | `POST /api/dependency-runs/{run_id}/retry`, `{"node_keys":["child"]}` |
| Release shared consumer | `POST /api/dependency-runs/{run_id}/release`, `{"session_id":42}` |
| Child-first teardown | `POST /api/dependency-runs/{run_id}/destroy` |

`plan_id`, `run_id`, and the plan's `run_id` are identical. Staging the same JSON
and target is idempotent; changing either under the same ID returns conflict.
Unknown versions and absent capability block execution. Status includes fixed
state/error codes and ownership identities, never resolved outputs. Malformed
request and catalog errors are sanitized, including FastAPI validation failures.

## Readiness and lifecycle

The parent is never released by HTTP acceptance, future scheduling, or aggregate
counts alone. The worker checks Workshop/WorkshopProvision ownership and UIDs,
the exact Workshop `status.resourceClaims` mapping and claim UID/Workshop UID
label, successful active claim state, and successful Anarchy provision job state.
Every required binding is resolved before any child is ordered; the exact parent
is reread immediately before each child submission. Each child's own start and
end window remains authoritative. Only the requested exact CatalogItem namespace
and its complete parameter schema are used, with no fallback catalog search.

Parent deletion requires an expiry hold that existing expiring providers cannot
provide. Preflight recursively inspects exact ResourceProviders in the explicitly
configured Poolboy namespace and refuses any default/max/relative lifespan or
auto-detach policy, dynamic references, cycles, and graphs beyond eight providers.
At runtime the claim and its reverse-linked ResourceHandle must have no effective
expiry/detachment state. Parent Workshop/WorkshopProvision end times are omitted
only after this preflight succeeds. An ordinary provider with a 48-hour default
fails closed with `parent_expiry_hold_unverified`; removing the requested parent
end alone does not solve autonomous expiry.

Cancellation retains existing resources. An explicit failed-child retry cleans
and verifies exact child claims and handles absent, advances the durable attempt
and submission token, and preserves the ready parent. Ambiguous submissions and
partial ownership inventories require attention and cannot be blindly retried.
Child handle identities are saved during provisioning and before deletion; a missing handle reference,
replacement UID, or lingering handle holds the parent. Shared parents also wait
for each registered consumer to release. Successful children settle through
teardown at their individual end times while later children retain the parent.
At the last successful child end, the run releases expired consumers together
and completes child-first parent teardown.
Failed, unknown, and incomplete attempts retain resources and evidence.

Legacy API operations that discover Workshops by catalog name reject schedules
overlapping resources owned by a dependency run. Clearing ordinary session/job
snapshots cannot remove the separate dependency database or its ownership ledger.
Start/stop/extend and parent replacement are explicitly unavailable in version 1.

## Offline validation and acceptance

Run `python3 -m pytest -q`, frontend `npm test`, and frontend `npx tsc --noEmit`.
The checked-in fixture and normalized-source SHA256 manifest pin the Labagator
contract/output/parameter parity. The integration harness drives the real SQLite
store, reconciler, and Babylon adapter with synthetic controller transitions,
including credentials and delayed ResourceHandle deletion. It performs no live IO.

Live enablement still requires explicit approval and evidence: the actual
Poolboy provider graph and output/resource names, parent-ready and child-order
timestamps, exact Workshop/claim/handle UIDs, mapped key presence without values,
successful child exercises, restart recovery, and child-before-parent teardown.
Do not use a production LB1663 record as an implicit migration or acceptance run.
