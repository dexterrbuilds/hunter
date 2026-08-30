# Execution providers

Milestone 3 adds opt-in delivery adapters without changing Pump.fun instruction
construction or trading strategy. If `execution` is absent, Hunter uses the
Milestone 2 solana-py `sendTransaction` path. Standard Solana JSON-RPC remains
the baseline and only provider configured in the shipped bot examples.

The provider-facing contract accepts `SignedTransaction` plus immutable
`ExecutionContext` and returns a normalized `SubmissionResult`. Submission is
only transport acknowledgement; confirmation and on-chain success remain
separate.

## Provider adapters

- `standard_rpc` submits base64 wire bytes using Solana `sendTransaction`.
  Arbitrary vendors can be configured; provider names are user-defined.
- `helius_sender` implements the documented Helius Sender `sendTransaction`
  contract with `skipPreflight: true` and `maxRetries: 0`. Hunter requires an
  explicitly tipped variant, exactly one tip instruction, a positive CU price,
  and configured tip bounds. It does not call a provider SDK that rebuilds the
  trade. See [Helius Sender documentation](https://www.helius.dev/docs/sending-transactions/sender).
- `helius_sender_max` isolates the maximum-performance Sender path. It accepts
  only an explicit `sender_max_tipped` variant with exactly one bounded tip and
  a positive CU price, supports provider `/ping` warm-up, records its region,
  and uses the same normalized acknowledgement/error lifecycle.
- `triton_jet` and generic `swqos` send an ordinary signed transaction through
  a provider-issued Solana-compatible Cascade/Yellowstone Jet endpoint. They do
  not invent a public Triton endpoint or proprietary request fields. See
  [Triton Cascade](https://docs.triton.one/chains/solana/cascade).
- `jito` implements Jito Block Engine's single-transaction
  `/api/v1/transactions` method. Normal relay, a tipped transaction, and
  `bundleOnly=true` are distinguished in telemetry. `bundleOnly` requires at
  least Jito's documented 1,000-lamport bundle tip. Multi-transaction
  `sendBundle` is not implemented because Hunter has no atomic multi-transaction
  Pump.fun use case in this milestone. See [Jito low-latency transaction documentation](https://docs.jito.wtf/lowlatencytxnsend/).

Endpoints and headers may contain credentials in memory, but telemetry stores
only stable, sanitized endpoint IDs. Provider-native diagnostic text is
redacted before it is retained.

## Signed identity and variants

`single`, `race`, `hedged`, and `fallback` all relay the exact same wire bytes,
blockhash, and signature to compatible transports. A timeout never causes
Hunter to rebuild or re-sign an economically equivalent trade. Duplicate-
signature responses are retained as ambiguous evidence and do not authorize a
replacement. A provider's “already processed” response advances Hunter to
confirmation of the existing signature; it is not treated as proof of success
or as permission to sign again.

A tip changes the transaction message, so it is a separate execution variant:
`jito_tipped`, `helius_sender_tipped`, or `sender_max_tipped`. Hunter constructs that variant once,
signs it once, and can relay that one signature across compatible transports.
It does not mix tipped and untipped messages inside one race.

Every adapter also declares normalized capabilities: accepted variants,
standard signed-message support, same-signature race support, tip and CU-price
requirements, and transport features such as SWQoS, Sender Max, Jito, or direct
leader routing. A race/hedge containing known incompatible capabilities is
rejected before any provider call.

## Broadcast modes

- `single`: use the highest-priority submit endpoint.
- `race`: concurrently relay the same signed transaction. Return the first
  acceptable acknowledgement while remaining attempts finish for telemetry.
- `hedged`: send to the primary, then relay the same transaction to secondaries
  only if the hedge delay expires without an acceptable acknowledgement.
- `fallback`: try the next transport only after a classified transport,
  availability, authentication, rate-limit, or leader-routing failure. It
  stops on duplicate-signature, on-chain, and ambiguous outcomes.

## Role separation and connections

Standard RPC endpoints can independently carry `account_read`, `blockhash`,
`submit`, `confirm`, and `websocket` roles. Helius Sender and Jito are submit-
only. Hunter maintains reusable solana-py clients and aiohttp keepalive
sessions per configured path; documented provider ping endpoints may be warmed
at startup. Provider-attempt telemetry records whether Hunter created or reused
its HTTP session and the local session generation. This is not proof that an
intermediary reused a particular TCP socket. TLS verification is never disabled.

Optional `execution.latency_budgets` thresholds cover detection processing,
quote generation, blockhash retrieval, transaction build, signing, and
submission RTT. Exceeding one records and logs a warning after the execution;
it does not reject or delay the trade.

Provider health keeps a bounded rolling window of acknowledgement RTT, status
RTT, success, transport/rate-limit errors, and observed landing slots. Single,
hedged, and fallback routing preserve configured priority until every candidate
has at least 20 recent samples; only then may recent success rate and median RTT
reorder the full candidate list. No transient failure permanently excludes an
endpoint. Race mode continues to submit to all configured candidates.

See [execution-providers.example.yaml](execution-providers.example.yaml) for a
disabled, placeholder-only reference. Provider performance must be measured
from Hunter's own host, region, workload, and fee policy. Hunter does not claim
that any provider is universally fastest.

## Fees and risk

CU priority fee and Jito/Helius tip remain separate. Telemetry also keeps base
network fee, ATA/rent effects, and other known costs separate. Provider tip
minimum/maximum checks occur before bytes leave Hunter. The combined fee cap
includes known base, priority, tip, rent, and other SOL costs; an unknown base
fee remains unknown. Existing `risk.enforce` controls remain in force and now
include configured tips. Tipped execution requires active risk enforcement,
fail-closed unknown-base-fee handling, positive provider minimum/maximum tip
bounds, and a combined fee cap.
