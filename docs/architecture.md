# Hunter architecture

Milestone 2 introduces durable domain and application boundaries while keeping
the audited standard Solana JSON-RPC path active. Pump.fun instruction builders,
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
       standard Solana JSON-RPC

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

The standard RPC path records UTC correlation timestamps and monotonic
nanosecond timestamps for request, build, sign, submit, RPC response, signature,
and observed commitment stages. It also records sanitized provider/endpoint
identity, slots, blockhash validity, transaction size, compute limit/price,
priority fee, signature, and typed error. Completed snapshots are persisted
after the confirmation hot path; credential-bearing endpoints are never stored.
