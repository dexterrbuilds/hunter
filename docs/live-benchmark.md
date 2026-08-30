# Controlled live benchmark

This command can spend real funds. It exists to measure Hunter's complete path
from a known observation or manually selected mint to transaction landing. It is
not a token-selection strategy and never runs as part of normal `hunter` startup.

No live transaction is sent during installation, automated testing, or ordinary
benchmark reporting.

## Use a benchmark wallet

Create a separate, low-balance wallet used only for these trials. Do not reuse a
production trading wallet. Set `benchmark.dedicated_wallet: true` only after the
private bot configuration actually points to the separate wallet; Hunter cannot
prove wallet ownership or purpose. No private key is stored in the benchmark
database.

Give the private benchmark bot configuration its own `storage.database_path` as
well. That database carries the durable logical execution/signature and acquired
position needed for restart-safe idempotency and an auditable manual exit.

Private YAML, `.env`, authenticated endpoint URLs, and wallet keys must remain
outside Git. The example below is a shape, not a ready-to-run configuration.

## Authorization is deliberately redundant

An economic trial requires all of the following:

1. invocation of the isolated `hunter-benchmark-live` command;
2. `benchmark.live_enabled: true` in the selected private configuration;
3. the exact acknowledgement text shown below;
4. the command-line `--allow-live` flag;
5. `risk.enforce: true` plus valid normal risk limits;
6. every benchmark-specific cap explicitly present and positive;
7. an explicit mint, raw quote amount, and route ID.

No environment-variable default enables live mode. If one condition is missing,
Hunter refuses before creating an execution.

```yaml
benchmark:
  live_enabled: false
  acknowledgement: ""
  region_label: "frankfurt-vps"
  dedicated_wallet: true
  warm_providers: true
  mint: "REPLACE_WITH_APPROVED_PUMP_MINT"
  quote_mint: "sol"
  quote_amount_raw: 50000
  # Optional only when independently known from authoritative creation data:
  # authoritative_launch_slot: 123456789
  # authoritative_launch_timestamp: "2026-01-01T00:00:00Z"
  # detection_slot: 123456790
  provider_matrix:
    - id: rpc-primary
      providers: [rpc-primary]
      mode: single
      execution_variant: standard
    - id: rpc-race
      providers: [rpc-primary, rpc-secondary]
      mode: race
      execution_variant: standard
    - id: jito-tipped
      providers: [jito]
      mode: single
      execution_variant: jito_tipped
  exit_policy: manual
  # Other choices: sell_immediately_after_confirmed_buy, sell_after_seconds
  # exit_after_seconds: 10
  caps:
    # These must also contain the slippage-expanded instruction maximum.
    maximum_sol_spend_per_trade_lamports: 75000
    maximum_quote_amount_raw: 75000
    maximum_live_trades: 1
    maximum_cumulative_spend_raw: 75000
    maximum_priority_fee_lamports: 10000
    maximum_tip_lamports: 10000
    maximum_combined_transaction_cost_lamports: 30000
    minimum_wallet_reserve_lamports: 10000000
    maximum_duration_seconds: 60
```

To acknowledge the risk in a private config, the value must be exactly:

```text
I UNDERSTAND HUNTER LIVE BENCHMARK USES REAL FUNDS
```

Before execution, Hunter prints the maximum configured financial exposure. An
amount above either the raw quote cap or SOL per-trade cap is rejected. Priority
fee, tip, combined fee, wallet reserve, cumulative spend, count, and duration
limits are independent of normal trading settings; where both define a maximum,
the stricter value applies. An unknown base fee is rejected.

## Provider matrix and fair comparisons

Only enabled, credentialed providers named in the selected matrix row
participate. `single`, `race`, `hedged`, and `fallback` retain their documented
execution semantics. Race and hedge submit identical signed bytes where the
transport accepts them; multiple acknowledgements of one signature are one
economic purchase.

A Jito or Helius tipped message is materially different from an untipped
message. Hunter records it as a separate execution variant and requires a
separate economic trial. Never compare it by independently buying the same token
through every provider in one uncontrolled run.

The logical execution ID is stable across command restarts for the same route,
variant, and mint. If a prior submission is ambiguous, Hunter inspects its
persisted signature before constructing a replacement. Use a new route ID or a
new approved mint for a genuinely new sample; a timeout is not permission to
create a second purchase.

## Passive detection benchmark

Passive observation gathers real launch timing with zero trading exposure:

```yaml
detection:
  sources:
    - type: logs
      wss_endpoint: ${SOLANA_NODE_WSS_ENDPOINT}
    - type: blocks
      wss_endpoint: ${SOLANA_NODE_WSS_ENDPOINT}
    - type: geyser
      geyser_endpoint: ${GEYSER_ENDPOINT}
      geyser_api_token: ${GEYSER_API_TOKEN}
      geyser_auth_type: x-token
    - type: pumpportal
      pumpportal_url: wss://pumpportal.fun/api/data
```

Run it with `hunter-benchmark-detection`. Events are correlated by creation
signature when available, otherwise mint. It records source, observed wall and
monotonic time, launch/transaction/detection slots where provided, and relative
source delay. It never constructs or signs a transaction.

## Transport/read benchmark

`hunter-benchmark-transport` loads standard RPC providers from the `execution`
section and performs configured read-only probes over one reused HTTP session.
No submission adapter is called.

```yaml
transport_benchmark:
  probes: [health, blockhash, account, status, priority_fee]
  account_address: "PUBLIC_ACCOUNT_TO_READ"
  status_signature: "PUBLIC_TRANSACTION_SIGNATURE_TO_INSPECT"
  priority_fee_accounts: ["PUBLIC_WRITABLE_ACCOUNT"]
```

`health` and `blockhash` need no extra values and are the defaults. `account`,
`status`, and `priority_fee` require the explicit public inputs shown above.
Use `--warmup` to record a warm-up request before the measured probe sequence.
Without it, the first request is labeled `cold`; later requests are `warm`.
Failures, timeouts, authentication failures, rate limits, and RPC rejections
remain in the dataset. WebSocket observation timing belongs to the passive
detection benchmark rather than this HTTP probe command.

## Economic trial and exits

The mint is manual and the quote amount is exact raw units. Hunter disables the
extreme-fast path for this benchmark so current authoritative curve and fee
state are used. The normal Pump.fun builder, signer, idempotency, execution
routing, confirmation, transaction inspection, and telemetry path remain active.

Exit handling is explicit:

- `manual` leaves acquired tokens in the benchmark wallet and persists the buy;
- `sell_immediately_after_confirmed_buy` sells only after a confirmed buy;
- `sell_after_seconds` waits the configured duration, then requests the sell.

An exit consumes another benchmark trade-count allowance. Configure at least two
trades when selecting an automatic exit. Hunter does not introduce a background
dump strategy.

## Slots and latency

Hunter reports launch-relative and detection-relative results separately:

- **block 0** means `landed_slot == authoritative_launch_slot`;
- **launch +N** means `landed_slot - authoritative_launch_slot == N`;
- **same detection slot** means `landed_slot == detection_slot`;
- **detection +N** means `landed_slot - detection_slot == N`.

Same detection-slot landing is never called block 0 unless the authoritative
launch slot exists and is identical. PumpPortal timing may lack an authoritative
launch slot, so launch-relative classification can be unknown.

The stored timeline can include launch to detection, detection to quote, quote
to build, build and sign duration, submission RTT, submit to processed/confirmed/
landed, detection to landed, and launch to landed. Missing evidence stays null.

## Costs, failures, and interpretation

Base transaction fee, CU priority fee, provider tip, rent/ATA effects, and other
known SOL costs are separate fields. Protocol trading fees remain in the quote
asset. The report includes failed and ambiguous attempts; excluding them would
make latency results misleading.

Export aggregates as JSON or CSV from the credential-free SQLite source. Export
never includes endpoint URLs, authentication headers, wallet keys, or tokens.
Performance is geographic and time-dependent. Gather a meaningful sample in the
actual deployment region, compare cold and warm attempts separately, and review
cost per successful same-slot landing before changing production routing.
