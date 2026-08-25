# Known risks

Hunter remains pre-production software. This list distinguishes Milestone 2
improvements from risks that remain open; it is not exhaustive.

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

- **Single-provider dependency remains.** Reads, submission, inspection, and
  confirmation use the configured standard Solana JSON-RPC endpoint.
- **No alternate observer or rebroadcast service exists.** Ambiguous outcomes
  intentionally stop/reconcile rather than guessing; this favors duplicate-sale
  safety over automatic liveness.
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
- **Dynamic priority fees add a hot-path RPC read.** No latency optimization or
  provider benchmark is claimed in Milestone 2.

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
- **Logging and local SQLite writes are not benchmarked.** Identity persistence
  is correctness-critical; broader latency work belongs to a later milestone.

## Secret handling

- Redaction is defense in depth, not permission to log secrets. New paths must
  avoid credential-bearing values and register configured secrets.
- SQLite stores no private keys, RPC URLs, Geyser tokens, or Telegram secrets.
- Generated databases, logs, `.env*` files (except `.env.example`), keypairs,
  and token/session files remain ignored and must not be committed.
