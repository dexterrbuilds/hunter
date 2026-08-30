# Known risks

Hunter remains pre-production software. This list distinguishes implemented
improvements from risks that remain open; it is not exhaustive.

## Resolved or materially improved in Milestone 3.6

- Multiple creation feeds now converge on one bounded earliest-event aggregator;
  the first valid observation claims the mint and later/replayed observations
  remain telemetry-only.
- RabbitStream and Triton Riptide are isolated adapters over the generic
  Yellowstone parser, with region, ingress, signature, slot, parse, and
  validation timing.
- Raw shred ingestion has bounded UDP queues/workers and a strict provider
  SDK/sidecar reconstruction boundary; unknown packets are never guessed.
- Pump.fun zero-read eligibility is an explicit fail-closed confidence model and
  records missing state rather than silently assuming it.
- Maximum-performance startup uses a strict blockhash age, faster background
  refresh, sender warm-up, and explicit required/optional readiness states.
- Helius Sender Max and Triton Jet/SWQoS have isolated adapters and declared
  execution capabilities. The router rejects incompatible message variants
  before dispatch.
- One economic variant is signed/serialized once and reused across compatible
  transports. New tests cover simultaneous detector arrival and incompatible
  sender races.
- Detection, shred, claim, and telemetry queues are bounded and expose drop
  counts instead of growing indefinitely.

## Remaining maximum-performance risks

- **Native Triton raw-shred reconstruction needs the provider SDK or a reviewed
  colocated sidecar.** Hunter supplies the bounded ingress and strict sidecar
  envelope, not an invented Solana shred decoder. The safe example leaves it
  disabled.
- **The vendored Yellowstone protobuf predates Triton's beta
  `SubscribeDeshred`.** Riptide uses ordinary processed transaction updates
  until the canonical protobuf is deliberately refreshed and characterized.
- **Feed connection readiness is currently local to each reconnecting adapter.**
  Startup validates feed configuration and sender/blockhash readiness, but a
  durable runtime readiness dashboard has not been added.
- **Claiming is single-node.** It is concurrency-safe within one Hunter process;
  running multiple trading replicas against the same wallet needs an external
  coordination design before it can be safe.
- **TTL expiry is not replacement authority.** The feed claim cache is bounded,
  while durable execution/idempotency state remains the authority. Operators
  must retain the SQLite database across restarts.
- **Queue overflow drops the newest observation.** Drops are counted, but severe
  sustained overload can hide a launch or a later correlation sample. Tune from
  real traffic rather than removing bounds.
- **Maximum-performance has not been live benchmarked by this project.** No
  provider is known to be fastest from the operator's host until measured.
- **Regional endpoint semantics and tip accounts can change.** Provider-issued
  configuration must be rechecked; Hunter does not scrape hot-path metadata.
- **Processed observations are not finalized truth.** A low-latency fork can be
  abandoned. The profile exposes this risk rather than strengthening commitment
  silently.
- **Economically critical SQLite identity writes remain synchronous.** Removing
  that boundary would weaken duplicate-trade safety; storage latency should be
  measured on the production disk.

## Resolved or materially improved in Milestone 3

- Standard RPC, Helius Sender, Jito single-transaction delivery, and multiple
  generic RPC transports have isolated adapters and normalized results.
- Compatible broadcast transports reuse one signed transaction identity;
  tipped transactions are explicitly separate variants.
- Single, race, hedged, and classified fallback routing preserve Milestone 2
  idempotency and never rebuild solely because a provider timed out.
- Account-read, blockhash, submit, confirmation, and WebSocket roles can use
  separately configured standard endpoints.
- Provider acknowledgement, landing, detector timing, blockhash age, fee
  provenance, and slot-distance metrics are persisted without synchronous
  telemetry writes in the submission path.
- Fixed, cached dynamic, periodically refreshed dynamic, and provider-estimate
  priority-fee boundaries are available; existing fixed/dynamic settings remain
  compatible.
- Jito/Helius tips are bounded, counted separately, limited to one instruction,
  and included in known combined fee exposure.
- Provider health uses bounded rolling evidence, refuses to use tiny samples,
  and only reorders non-race candidates when every endpoint has enough recent
  data; no endpoint is permanently excluded.

## Resolved or materially improved in Milestone 2

- Positions, fills, lifecycle transitions, logical executions, attempts, and
  telemetry are persisted in versioned SQLite storage.
- Startup reconciles active inventory and never sells solely because persisted
  and wallet balances differ.
- Normal Pump sells use curve output, price impact, current fee state, and an
  integer slippage floor instead of reference-price multiplication.
- Realized accounting uses observed execution effects, supports partial exits,
  and keeps SOL costs separate from SPL-quote PnL.
- Exact Pump transaction amounts use typed raw integers and explicit rounding;
  unsupported quote assets fail safely.
- Blockhash age/expiry and last-valid height are tracked and checked.
- Ambiguous signatures have durable identity and are inspected before any
  economically equivalent replacement.
- Trading, kill-switch, size/exposure, fee, wallet-reserve, and rate guards are
  available and actively enforced when `risk.enforce` is enabled.
- Continuous detection and position monitoring use separate bounded workers;
  duplicate detections are claimed before enqueue.

## Remaining funds and accounting risks

- **Risk enforcement is opt-in for compatibility.** Existing configurations
  continue with `risk.enforce: false`; operators must deliberately enable and
  calibrate raw-unit limits.
- **Legacy paths still use floats.** LetsBonk and Pump extreme-fast sizing retain
  Milestone 1 floating-point behavior. Normal Pump plans use exact integers.
- **Strategy monitoring still exposes decimal views.** Persisted raw accounting
  is exact, but inherited TP/SL display and monitoring objects convert to float.
- **Some execution costs remain unknowable.** Providers may omit event/log or
  balance detail; unknown fees stay explicit. Cleanup transactions are not yet
  allocated to a position.
- **SPL-quoted net PnL needs FX data.** SOL network/rent costs are not converted
  to USDC or another quote asset, so net quote PnL remains unknown when needed.
- **No portfolio loss/drawdown policy exists.** Milestone 2 guards exposure and
  fees but does not implement daily loss, drawdown, or price-oracle controls.
- **Local key material remains in process memory.** No hardware, encrypted, or
  remote signer is implemented.

## Remaining execution and recovery risks

- **Alternate providers are opt-in and unbenchmarked locally.** Shipping an
  adapter does not prove it is faster or more reliable from the operator's
  region. Standard RPC remains the default.
- **Jito multi-transaction bundles are not implemented.** `bundleOnly` is the
  documented single-transaction mode; Hunter has not established an atomic
  multi-transaction Pump.fun requirement.
- **Tip account selection is operator-configured.** Hunter does not fetch Jito
  tip accounts synchronously on the trade hot path. A stale/invalid configured
  account causes provider rejection rather than being guessed.
- **Tip attribution after an interrupted confirmation can require review.** The
  immediate execution path records a configured delivery tip separately from
  rent. If Hunter restarts before that telemetry snapshot is durable and the
  operator changes the delivery configuration, the wallet's native balance
  effect still preserves total accounting cost, but automatic reconstruction
  may not be able to label the old transfer as tip versus another native cost.
- **Submission slot is an observation bound.** It is read asynchronously after
  acknowledgement and may be later than actual provider ingress. Landing slot
  is authoritative; ingress timing is not.
- **Confirmation is still RPC-observed.** Multi-provider submission does not
  make temporary status/metadata invisibility conclusive. Ambiguous outcomes
  still stop/reconcile rather than authorize a fresh signature.
- **A crash between chain landing and signature persistence is still possible.**
  Hunter persists immediately after the RPC response, but no local system can
  durably record a response it never received. Wallet reconciliation prevents
  automatic selling, but operator review may be required.
- **Unlinked pending buys need a repeated detection or operator recovery.** Buy
  identities are durable and prevent duplicate resubmission for the same mint,
  but a crash before position creation can leave a confirmed buy without enough
  persisted token metadata for fully automatic reconstruction.
- **Position aggregate and fill journal updates are separate SQLite
  transactions.** The aggregate is authoritative and survives, but a crash
  between writes can leave an incomplete audit journal requiring repair.
- **Confirmation relies on RPC semantics.** Temporary `getTransaction`
  invisibility is handled, but prolonged provider inconsistency can remain
  accepted-but-not-observed.
- **Synchronous dynamic priority mode still adds a hot-path RPC read.** Cached
  and periodic modes avoid it, but must be enabled and measured by the operator.

## Remaining protocol and throughput risks

- **Base token decimals remain six on the Pump compatibility path.** This is an
  explicit Pump assumption, not a general token invariant.
- **Fee correctness depends on current on-chain state and vendored definitions.**
  Missing/malformed Global or FeeConfig state fails closed; a protocol upgrade
  may require an IDL/SDK review before Hunter can trade safely.
- **Extreme-fast mode cannot produce a reserve-pinned quote.** Enforced risk
  mode rejects paths without an exact plan; otherwise it retains characterized
  behavior.
- **Default token age is extremely small.** `0.001` seconds can reject events
  after ordinary scheduling delay.
- **Listener completeness varies.** Some listener/parser paths may miss events
  or omit metadata; PumpPortal still requires authoritative chain refresh.
- **Durable identity writes remain synchronous.** Signature persistence is
  correctness-critical and intentionally precedes confirmation. Diagnostic
  telemetry and ordinary log I/O are asynchronous, but SQLite/device latency
  still needs measurement on the deployment host.
- **Detector latency can dominate sender latency.** PumpPortal has no
  authoritative event slot and must refresh chain state; Geyser/log/block
  observations depend on the chosen provider and network path.
- **No real transaction benchmark has been run by the project.** Milestone 3.5
  adds guarded preparation and passive/read-only measurement, but offline tests
  still prove only routing and safety semantics—not mainnet landing performance.
- **Live benchmarks remain financially risky.** Slippage, adverse selection,
  token behavior, provider failure, and sell failure can lose the entire tiny
  benchmark amount. Caps bound exposure; they do not make a trade safe.
- **A dedicated-wallet declaration is operator supplied.** Hunter warns when it
  is false but cannot prove that a configured key is separate from production.

## Secret handling

- Redaction is defense in depth, not permission to log secrets. New paths must
  avoid credential-bearing values and register configured secrets.
- SQLite stores no private keys, RPC URLs, Geyser tokens, or Telegram secrets.
- Generated databases, logs, `.env*` files (except `.env.example`), keypairs,
  and token/session files remain ignored and must not be committed.
