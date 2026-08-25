# Known unresolved risks

These findings are intentionally unresolved in the Hunter Milestone 1 baseline.
The bot's existing behavior was preserved so later fixes can be made against
characterization tests. Do not treat this list as exhaustive.

## Funds and position safety

- **Positions are in memory.** A process restart loses position and exit state.
- **Sell retry and recovery are incomplete.** Time-based exits have no durable
  recovery path, and exhausted take-profit/stop-loss retries leave a position
  active but unmonitored.
- **Realized PnL is inaccurate.** Sell results use a supplied reference price
  rather than parsing actual proceeds and do not include network, protocol,
  priority, rent, or cleanup costs.
- **The sell minimum is reference-price based.** It does not calculate exact
  curve output, price impact, and protocol fees.
- **Monetary calculations use floats.** Conversion to raw units truncates and
  may expose binary floating-point edge cases.
- **No exposure controls exist.** There is no durable per-trade, per-token,
  daily-spend, aggregate-exposure, or loss limit.
- **No kill switch exists.** A user cannot atomically disable all trading and
  outstanding strategy activity through a dedicated safety control.
- **No total-fee guard exists.** The priority-fee hard cap limits the price per
  compute unit, not the transaction's total fee, rent, or full cost.
- **Balance sufficiency is not checked before construction.** SOL, quote-token,
  fee, and rent insufficiency are generally discovered during submission.

## Transaction delivery and confirmation

- **Blockhash freshness is not enforced.** The cache does not track retrieval
  age or last-valid block height and can be unavailable at startup.
- **Confirmation outcomes can be ambiguous.** Immediate transaction lookup may
  lag even after submission or confirmation, while accepted-but-dropped
  transactions are not distinctly represented.
- **Submission is single-provider.** Reads, submission, and confirmation depend
  on one configured RPC endpoint with no hedging or independent observer.
- **No active rebroadcast policy exists.** Transport retries reuse one signed
  transaction, while accepted-but-not-landed cases are not deliberately retried.
- **Dynamic priority fees add hot-path latency.** Estimation requires another
  RPC request and may not use exactly the same writable recipient selected for
  the final transaction.

## Detection and throughput

- **Trade processing is serial.** A token may occupy the main queue through
  buy, hold, and sell, delaying or dropping later detections.
- **The default maximum token age is extremely small.** `0.001` seconds is
  unrealistic once a token waits in a process queue.
- **Some listeners can miss or delay events.** Confirmed block subscriptions
  arrive later, the block parser can return only the first matching creation,
  and lookup-table account resolution is not uniform across runtime parsers.
- **Logging remains verbose.** Synchronous event-path logging may affect
  latency even though credential redaction is now applied to configured sinks.

## Protocol and configuration assumptions

- **Unsupported quote-token assumptions are unsafe.** Unknown quote mints
  default to nine decimals and legacy SPL Token behavior. Hunter should reject
  them until metadata and support are explicit.
- **Base token decimals are fixed at six.** This matches the current Pump.fun
  path but is not a general token invariant.
- **Fee-recipient selection can create contention.** The baseline uses one
  normal or reserved fee recipient while randomizing the buyback recipient.
- **Creation-event token-program inference has a legacy edge case.** The log
  parser can infer Token-2022 when the exact create variant is not available.
- **Configuration validation is incomplete.** Several zero, range, endpoint,
  key-format, and compute/fee combinations are not rejected early.

## Secret handling

- **Local private keys remain supported.** The wallet keeps a base58 private
  key string and a parsed keypair in process memory.
- **Redaction is not a substitute for safe calls.** Newly added logging paths
  must avoid passing secrets in the first place and must register configured
  secret values before an error can be logged.
- **No encrypted or remote signer exists.** Hardware and delegated signing are
  future execution-layer work.
