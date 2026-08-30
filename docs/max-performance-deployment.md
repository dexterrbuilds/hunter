# Maximum-performance deployment

Hunter's `maximum_performance` profile is an opt-in production infrastructure
path for measuring and reducing detection-to-dispatch latency. It does not
change token selection, trade amounts, Pump.fun instruction semantics, curve
economics, or exit strategy.

It is not a guarantee of same-slot landing. Results depend on the deployment
region, feed arrival, provider connectivity, leader schedule, account
contention, fee settings, and Solana network conditions. Measure from the
actual host using Hunter's benchmark tools before selecting a route.

## Recommended topology

```text
Pump.fun creation
        |
        +---- RabbitStream (Amsterdam) ------+
        +---- Triton shreds / sidecar -------+--> earliest valid event
        +---- Triton Riptide (processed) -----+             |
        +---- ordinary fallback feeds --------+             v
                                                       mint claim
                                                           |
                                            authoritative-state assessment
                                                           |
                                    +----------------------+----------------+
                                    |                                       |
                           complete event state                    minimum chain refresh
                           zero hot-path read                       (never guess state)
                                    |                                       |
                                    +----------------------+----------------+
                                                           v
                                             build / sign / serialize once
                                                           |
                              +----------------------------+----------------+
                              |                            |                |
                       Helius Sender Max             Triton Jet/SWQoS   compatible RPC
                              |                            |                |
                              +--- identical bytes/signature race ----------+

Direct Jito Amsterdam is a separately selected tipped message variant when its
message requirements differ. Hunter never constructs multiple independent buys
merely because several transports are configured.
```

Triton advertises Pro Trading Centers in Amsterdam and Tokyo. RabbitStream and
Jito publish Amsterdam endpoints, and Helius publishes a regional Amsterdam
Sender path. Amsterdam is therefore a practical common region for the services
in this profile, not a universal speed claim. Every region remains a config
value; deploy elsewhere when measured results support it.

## Configuration

Start with
[`config/examples/hunter-maximum-performance.yaml`](../config/examples/hunter-maximum-performance.yaml).
The file is deliberately non-trading:

```yaml
enabled: false

infrastructure:
  profile: maximum_performance
  region: amsterdam
  allow_degraded: false

risk:
  enforce: true
  trading_enabled: false
  emergency_kill_switch: true
```

Copy it to private YAML, reference secrets through environment interpolation,
and keep both the trading switch and kill switch closed until configuration,
passive detection measurements, and read-only provider checks have passed.

`maximum_performance` requires:

- `filters.listener_type: aggregate`;
- at least one enabled feed;
- active risk enforcement;
- a fresh cached blockhash within the configured age budget;
- every required sender to warm successfully unless degraded operation was
  explicitly allowed.

The profile never turns trading on. It does not infer endpoints, credentials,
tip accounts, amounts, or fee caps.

## Detection feeds

### RabbitStream

RabbitStream is configured as a dedicated listener but uses Hunter's generic
Yellowstone protobuf/parser path because Shyft documents it as a drop-in
Yellowstone gRPC replacement. Network ingress is timestamped before Pump.fun
parsing. The observation carries source, region, slot, signature when present,
parser completion, and validation completion.

Use `processed` commitment for minimum detection latency in this profile unless
the operator deliberately selects a stronger commitment. A processed event can
still disappear from the finalized fork; trading risk must account for that.

### Triton Riptide / Dragon's Mouth

Riptide remains a generic Yellowstone adapter with Triton-specific endpoint and
region configuration isolated in the listener wrapper. Hunter requests
`processed` by default. Current ordinary `Subscribe` support uses the vendored
Yellowstone protobufs.

Triton's newer `SubscribeDeshred` stream is an earlier decoded-transaction path.
The checked-in protobuf does not currently include that beta method, so Hunter
does not fabricate a request. Regenerate the single canonical protobuf copy
from a reviewed upstream version before enabling native `SubscribeDeshred`.

### Triton shreds

Raw Solana shreds cannot be reconstructed correctly by concatenating UDP
packets. Reconstruction is erasure-code, slot, fork, and transaction-boundary
aware. `monitoring.performance.shred_ingress` therefore defines a strict
`ShredReconstructor` boundary for a Triton SDK or colocated reconstruction
sidecar.

The supplied `FramedTransactionReconstructor` decodes Hunter's explicit sidecar
envelope only:

```text
HNTR | slot:u64-le | signature:64 bytes | tx_length:u32-le | wire transaction
```

It is not a raw Solana shred decoder. Malformed, partial, or unknown frames are
dropped rather than guessed. The UDP ingress timestamps packets immediately,
uses a bounded queue and bounded parser workers, and tracks packets, drops,
malformed frames, reconstructed transactions, launches, and reconnects.

The safe example leaves `triton_shreds` disabled until an SDK/sidecar recognizer
is explicitly supplied.

### Ordinary fallback feeds

The aggregator can also consume existing Yellowstone, logs, blocks, and
PumpPortal listeners. PumpPortal remains fail-closed: its payload is not treated
as complete authoritative Pump.fun state and still triggers the existing
batched state refresh.

## Earliest valid event and duplicate prevention

All enabled feeds report to one `EarliestEventAggregator`; detectors do not call
the trading engine independently. Observations correlate by mint, enriched by
creation signature and launch slot when later sources supply them.

The single-node state machine is:

```text
UNSEEN -> OBSERVED -> CLAIMED -> TRADE_REQUEST_CREATED
```

The first valid observation claims the mint. Later and replayed observations
remain attached for latency comparison but cannot create another trade request.
Claims live in a TTL-bounded cache. Hunter's durable Milestone 2 logical
execution ID remains the economic idempotency authority after the callback.

For compatible sender races, one logical buy means one message, one blockhash,
one signature, one serialization, and many transports carrying the same bytes.
A timeout is not replacement authority. A distinct new signature remains
subject to the persisted expiry/recovery lifecycle.

## Authoritative fast path

`FastPathConfidence` makes the pre-buy state decision explicit:

- `AUTHORITATIVE_EVENT_STATE`: canonical event state contains all required
  mint, curve, associated curve, creator/vault, token program, quote mint, and
  quote token program fields;
- `AUTHORITATIVE_WITH_CACHED_STATIC_STATE`: reserved for versioned immutable
  state proven safe to combine with the event;
- `REQUIRES_REFRESH`: a critical field is absent or the source is not trusted;
- `UNSUPPORTED`: the path is outside the Pump.fun optimization boundary.

Only accepted authoritative categories may skip hot-path account reads. Setting
`trade.trust_create_event: false` always forces the existing refresh. Hunter
records the confidence category and missing fields in execution telemetry.

This classification does not make Pump.fun extreme-fast sizing reserve-pinned;
that inherited limitation remains documented in `known-risks.md`.

## Background execution state

### Blockhash

The maximum-performance profile runs the existing blockhash updater every 750
milliseconds and enforces the smaller of the execution and infrastructure age
budgets. Context includes blockhash, source provider, source slot, fetch time,
and last-valid block height. Known-expired or over-age values are not used.

The reusable `BlockhashCacheRefresher` contract supports other provider-neutral
blockhash sources. Its fast lookup has no network I/O; `get_or_refresh` is the
explicit safe fallback when no usable cached value exists.

### Priority fee

Use `periodic_dynamic`, `cached_dynamic`, or a bounded fixed fee to keep
estimation off the trade hot path. Every selection retains estimate source,
age, latency, CU price, compute limit, and maximum priority-fee exposure. The
risk engine and hard cap remain authoritative.

### Jito tip

`JitoTipCache` refreshes an optional estimate in the background and clamps every
selection to explicit minimum and maximum lamports. It records source, strategy,
age, and estimation latency. A Jito tip remains separate from the CU priority
fee and cannot exceed combined fee/risk limits.

## Submission capabilities and variants

Each provider declares message capabilities instead of being grouped by brand:

- accepts a standard signed transaction;
- permits an identical-signature race;
- requires a tip;
- requires a positive CU price;
- accepted execution variants;
- standard RPC, SWQoS, Sender Max, Jito, direct-leader, tipped-message, and
  same-signature features.

The router validates the chosen variant before any bytes leave Hunter. A race
or hedge is rejected when all known adapters do not accept the same already
signed variant.

### Helius Sender Max

`helius_sender_max` uses documented `sendTransaction` JSON-RPC semantics with
base64 encoding, `skipPreflight: true`, and `maxRetries: 0`. Hunter requires an
explicit `sender_max_tipped` message, exactly one configured tip instruction, a
positive CU price, provider tip bounds, combined fee approval, and a warm-up
endpoint. The operator must use the current endpoint and tip accounts issued or
documented by Helius.

### Triton Jet / SWQoS

`triton_jet` and generic `swqos` adapters use the provider's Solana-compatible
`sendTransaction` endpoint. Triton's current public product name is Cascade;
Yellowstone Jet is its forwarding component. Endpoint and authentication
details are supplied during provider onboarding, so Hunter does not invent a
public URL or header.

These adapters can relay the exact signed bytes already accepted by a standard
RPC or compatible Sender variant. Provider-side shielding or proprietary
parameters must be reviewed before adding them, because message/routing changes
can invalidate same-signature assumptions.

### Direct Jito Amsterdam

The existing Jito adapter supports the Amsterdam block-engine transaction
endpoint, ordinary single-transaction relay, and the documented
single-transaction `bundleOnly` query. A tipped Jito message remains an explicit
variant. Hunter does not construct uncontrolled multi-transaction bundles.

## Readiness and degraded state

Before enabling detection, maximum-performance startup loads the signer,
checks blockhash freshness, starts fee state, constructs sessions, and warms all
configured senders. The profile refuses configuration without enabled execution
routing, an aggregate listener, an enabled feed, and active risk enforcement.
Components are `READY`, `WARM`, `DEGRADED`, or `NOT_READY`.
Aggregate Hunter state is:

- `MAXIMUM_PERFORMANCE`: required and optional startup components are ready;
- `DEGRADED`: at least one configured component is unavailable;
- `NOT_READY`: a required component failed and degraded operation is disallowed.

`allow_degraded: false` is recommended. A slower fallback is never silent: the
component report includes the safe reason class without endpoint credentials.
Feed runtime reconnect status is tracked at the adapter level; persistent
readiness export is still listed as a remaining integration risk.

## Backpressure and persistence

- aggregate observations, UDP packets, and telemetry have hard queue limits;
- feed loops use non-blocking enqueue and count dropped observations;
- claims have TTL bounds;
- shred parsing uses bounded workers;
- execution telemetry SQLite writes remain on the background sink;
- economically critical identity/signature persistence remains synchronous by
  design, because dispatching before durable idempotency state would be unsafe.

When a queue is full, Hunter drops the newest diagnostic/feed observation and
increments its counter. It does not evict an existing economic claim. Capacity
must be tuned from observed traffic and drop metrics.

## Linux host recommendations

Hunter already uses `uvloop` on supported Unix environments and falls back to
standard asyncio when unavailable; macOS development remains supported. For a
production Linux host:

- prefer dedicated, high-clock cores over burst/shared vCPU;
- avoid CPU throttling and oversubscribed hosts;
- colocate with provider infrastructure only when measured latency justifies it;
- reserve a core for the event loop/parser workers where operational tooling
  supports it;
- set appropriate file/socket limits for persistent feeds;
- keep TLS verification enabled;
- monitor reconnects, queue drops, parser time, event-loop lag, and GC pauses;
- do not disable Python garbage collection globally without workload evidence.

Hunter does not set CPU affinity, kernel parameters, or GC policy automatically.

## Safe rollout

1. Keep `enabled: false`, `risk.trading_enabled: false`, and the kill switch on.
2. Validate config and run the complete offline suite.
3. Run passive detection correlation and read-only transport benchmarks.
4. Confirm readiness/warm states and zero queue drops under representative load.
5. Inspect the provider-issued tip accounts and current endpoint semantics.
6. Use a dedicated wallet and the Milestone 3.5 guarded benchmark flow for any
   explicitly authorized tiny economic trial.
7. Review detection, internal build/sign, acknowledgement, landing, failure, and
   cost distributions before changing production routing.

Never infer that one provider is fastest from marketing, one sample, or another
deployment region.

## Official references

- Shyft RabbitStream: <https://docs.shyft.to/solana-shredstreaming/how-to-stream-with-rabbitstream>
- Triton Dragon's Mouth and `SubscribeDeshred`: <https://docs.triton.one/project-yellowstone/dragons-mouth-grpc-subscriptions>
- Triton Cascade/SWQoS: <https://docs.triton.one/chains/solana/cascade>
- Triton Pro Trading Centers: <https://docs.triton.one/pro-trading-centers/introduction>
- Helius Sender: <https://www.helius.dev/docs/sending-transactions/sender>
- Jito low-latency transaction send: <https://docs.jito.wtf/lowlatencytxnsend/>
