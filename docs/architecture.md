# Hunter architecture

Milestone 3 adds measured provider routing around the Milestone 2 domain and
application boundaries. The audited standard Solana JSON-RPC path remains the
default when no `execution` section is configured. Pump.fun instruction builders,
account ordering, discriminators, PDA/ATA derivation, program IDs, and vendored
IDLs remain the Milestone 1 implementations.

## Dependency direction

```text
listeners / future interfaces
            |
      TradingEngine facade
            |
   BuyService / SellService -------- RiskService
            |                              |
      ExecutionPlan                 exposure + fee limits
            |
   ExecutionCoordinator
            |
 account | blockhash | builder | signer | submitter | confirmation | inspector
            |
         signed transaction identity
              /       |       \
       standard RPC  Helius  Jito
              \       |       /
            confirmation observer

strategies -> exit decisions -> SellService
positions <-> PositionService <-> SQLitePositionStore
```

`UniversalTrader` remains a compatibility coordinator during the incremental
split. Its active Pump path now uses the same raw quotes, risk checks, execution
effects, persistence, recovery, logical transaction identity, bounded token
workers, and bounded position monitors. New interfaces should depend on
`TradingEngine`, not on `UniversalTrader` or a listener.

## Raw amounts and rounding

`TokenAmountRaw`, `QuoteAmountRaw`, `Lamports`, `BasisPoints`, and
`MicroLamportsPerCU` represent integer units. Mint and decimals are explicit;
unknown quote assets do not default to nine decimals or the legacy token
program. Decimal conversion requires a declared rounding direction. Binary
floats are rejected by exact conversion APIs.

Pump transaction plans use raw integers. Legacy LetsBonk and Pump extreme-fast
sizing retain their characterized floating-point behavior because changing
those paths was outside this milestone. SQLite encodes raw integers as tagged
decimal text so the full unsigned 64-bit range round-trips exactly even through
older INTEGER-affinity schema columns.

## Pump.fun quotes and fees

Normal Pump buys and sells use integer virtual and real reserve state. A buy
deducts fee basis points from the quote budget using the official integer
ordering, calculates constant-product token output, caps output at real token
reserves, and applies the configured output slippage floor. The maximum quote
input preserves Hunter's configured-spend-plus-slippage behavior.

Pump's official SDK uses two fee-tier supply rules. Budget-to-token buy sizing
uses current mint supply. Exact-token trade fees and sells use the canonical
one-billion-token supply for non-mayhem curves and current supply for mayhem
curves. Protocol and creator fee components are ceiling-rounded separately.
Creator fees are removed only when authoritative curve state identifies the
default creator. Missing or malformed fee state fails closed; it is never
silently treated as zero.

Sell output is constant-product gross quote output minus separately rounded
protocol and creator fees, followed by the slippage floor. Curve, Global, and
FeeConfig accounts are read in one RPC context when supported, and the source
slot is retained in the quote.

## Execution and confirmation lifecycle

`ExecutionPlan` is immutable and carries a stable logical execution ID.
`executions` stores its current state; `execution_attempts` preserves every
submitted signature, blockhash, last-valid block height, and attempt number.
The signature is written immediately after the RPC response and before
confirmation is awaited.

Before replacing an ambiguous buy or sell, Hunter inspects the existing
signature. `NOT_OBSERVED`, timeouts, and dropped/unknown states do not authorize
a replacement. A replacement is allowed only after authoritative blockhash
expiry; an on-chain failure is permanent for that logical execution.

Confirmation distinguishes signature receipt, processed, confirmed, finalized,
on-chain failure, expiry, temporary metadata invisibility, timeout, and
dropped/unknown. A confirmed signature whose transaction metadata is briefly
invisible remains accepted-but-not-observed rather than being declared failed.

Cached blockhashes carry fetch time, monotonic age, source slot, and last-valid
block height. Hunter refreshes stale/expired values and refuses an RPC response
that is already expired. Submission retries do not reuse a signed transaction
after its known expiry.

## Execution effects and accounting

After a confirmed Pump transaction, Hunter reads transaction metadata and
wallet token/native balance changes plus the Pump `TradeEvent` when available.
`ExecutionResult` records actual token and quote deltas, network fee, priority
fee, observable protocol/creator fees, rent effects, slot, and explicit unknown
costs.

Position accounting uses actual raw quote cost/proceeds and average-cost
allocation for partial exits. Gross realized PnL is quote proceeds minus the
allocated quote cost basis. For SOL-quoted positions, known native entry, exit,
and other execution costs can be deducted for net PnL. Network fee already
contains the priority component, so priority fee is tracked separately for
analysis and is not deducted twice.

For an SPL quote such as USDC, SOL network/rent costs remain separately
denominated. Hunter does not perform implicit SOL-to-USDC conversion: net quote
PnL and realized return remain unknown when a nonzero or unknown SOL cost needs
conversion. Gross quote PnL remains available.

## Persistence and recovery

SQLite schema migrations persist position aggregates, fills, lifecycle events,
logical executions and attempts, telemetry, strategy/recovery metadata, and
settings. Secrets and endpoint URLs are not stored. WAL mode and a process-local
lock provide bounded local concurrency.

Startup loads active positions and inspects persisted sell signatures before
wallet reconciliation. Confirmed sells are applied from transaction effects;
known-expired sells become retryable; failed-on-chain sells become permanent;
ambiguous or identity-less sells become `RECONCILIATION_REQUIRED`. Hunter then
compares persisted inventory with wallet token balances. Missing accounts, read
failures, or mismatches require reconciliation and never trigger an automatic
sale. Eligible positions resume monitoring in a bounded worker pool. Closed
positions remain queryable but are not resumed.

## Position and strategy lifecycle

Persisted states are `OPEN`, `EXIT_REQUESTED`, `SELL_SUBMITTED`,
`SELL_CONFIRMED`, `SELL_FAILED_RETRYABLE`, `SELL_FAILED_PERMANENT`,
`RECONCILIATION_REQUIRED`, and `CLOSED`. Only the narrow typed retry taxonomy
permits another attempt. Retry exhaustion requeues monitoring instead of
forgetting the position.

Take-profit, stop-loss, trailing-stop, and timed strategy classes emit exit
decisions. They do not own transaction delivery. The inherited coordinator is
being migrated incrementally to this decision-to-service flow.

## Risk controls

`RiskService` is interface-independent and is invoked before signing on the
normal exact Pump path. When `risk.enforce` is true it enforces trading enabled,
emergency kill switch, maximum buy, maximum position size, aggregate exposure,
maximum priority fee, maximum known total fee/rent exposure, minimum SOL wallet
reserve, and a bounded trade rate. Successful active-path trades are recorded
for rate limiting.

Risk enforcement defaults off to avoid silently changing existing economics.
Example limits are conservative placeholders. Enabling enforcement on a path
without an exact execution plan fails closed rather than guessing exposure.

## Concurrency and telemetry

Continuous detection uses a fixed token-worker count. Token mints are claimed
before enqueue, preventing duplicate callbacks from creating duplicate logical
buys. Bought positions move to a separate fixed monitor pool, so holding one
position does not block detection. Monitor keys prevent duplicate ownership and
retry by requeueing within the same bounded pool. Shutdown cancels and joins
owned workers before closing persistence/RPC resources.

The standard RPC path and opt-in provider adapters record UTC correlation timestamps and monotonic
nanosecond timestamps for request, build, sign, submit, RPC response, signature,
and observed commitment stages. It also records sanitized provider/endpoint
identity, slots, blockhash validity, transaction size, compute limit/price,
priority fee, signature, and typed error. Per-provider attempts share one
signature unless a tip created an explicitly distinct variant. Completed
snapshots and provider attempts are queued in memory and persisted by a worker
after the submission hot path; credential-bearing endpoints are never stored.

Role-specific standard endpoints may serve account reads, blockhashes,
submission, confirmation, or WebSockets. Submission routing supports single,
race, hedged, and safe fallback modes. See
[execution-providers.md](execution-providers.md) and
[landing-metrics.md](landing-metrics.md).

## Maximum-performance infrastructure path

The optional `maximum_performance` profile adds a bounded layer above existing
listeners and below the trading coordinator:

```text
RabbitStream / Triton shreds / Riptide / fallback feeds
                            |
                  DetectionObservation
                            |
              EarliestEventAggregator
              correlation + mint claim
                            |
             one TokenInfo callback only
                            |
                    existing trader
                            |
              one prepared signed variant
                            |
         capability-validated provider routing
```

`DetectionIdentity` uses mint as the stable economic key and enriches it with
creation signature and launch slot when later feeds provide them. The
aggregator's process-local lifecycle is `UNSEEN -> OBSERVED -> CLAIMED ->
TRADE_REQUEST_CREATED`; durable logical execution and signature state remains
in SQLite. This division lets later observations improve latency data without
becoming a second buy authority.

Fast-path state is classified as authoritative event state, authoritative plus
versioned static state, requires refresh, or unsupported. The Pump.fun buyer
records that classification and skips state reads only when both the assessment
and `trust_create_event` permit it. Program instruction construction, fee/curve
math, PumpPortal refresh, and LetsBonk paths remain unchanged.

Submission adapters expose capabilities for message variants, tips, priority
fees, SWQoS/direct-leader routing, and identical-signature races. The router
rejects an incompatible race before dispatch. `ExecutionVariantPreparer`
formalizes one build/sign/serialization result per variant, while existing
Milestone 2 persistence remains the replacement authority.

Startup readiness distinguishes required and optional feeds/senders, blockhash
freshness, and signer initialization. A maximum-performance bot cannot enter
the listener loop when required components are unavailable unless degraded
operation was explicitly enabled. See
[max-performance-deployment.md](max-performance-deployment.md).
