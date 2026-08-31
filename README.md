<div align="center">

# HUNTER

### See it early. Price it exactly. Send it once.

Hunter is an independently maintained Solana trading bot built for new-token
detection and controlled execution on **Pump.fun** and **LetsBonk**.

<p>
  <a href="https://github.com/dexterrbuilds/hunter"><img alt="Hunter repository" src="https://img.shields.io/badge/GitHub-Hunter-111827?style=for-the-badge&logo=github"></a>
  <a href="https://t.me/dexterrbuilds"><img alt="Telegram @dexterrbuilds" src="https://img.shields.io/badge/Telegram-@dexterrbuilds-229ED9?style=for-the-badge&logo=telegram&logoColor=white"></a>
  <a href="https://x.com/dexterrbuilds"><img alt="X @dexterrbuilds" src="https://img.shields.io/badge/X-@dexterrbuilds-000000?style=for-the-badge&logo=x&logoColor=white"></a>
</p>

![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![Solana](https://img.shields.io/badge/Network-Solana-14F195?logo=solana&logoColor=black)
![License](https://img.shields.io/badge/License-Apache--2.0-blue)
![Status](https://img.shields.io/badge/Status-Pre--production-f59e0b)

</div>

---

> [!CAUTION]
> Hunter signs transactions and can control real funds. It is pre-production
> software, not a promise of profit or transaction inclusion. Start with an
> isolated wallet, small limits, and the risk engine enabled. Read
> [Known risks](docs/known-risks.md) before enabling a bot.

## What Hunter is

Hunter watches supported Solana event sources for newly created tokens, builds
the protocol transaction locally, signs it once, submits it through a selected
delivery path, and follows the resulting position through exit and accounting.

The project is being developed around four rules:

1. **Protocol correctness comes first.** Pump.fun account order, instruction
   data, PDAs, ATAs, program IDs, Token-2022 behavior, and vendored IDLs are
   protected by offline characterization tests.
2. **One trade has one identity.** Provider timeouts do not grant permission to
   rebuild and double-submit an economically equivalent transaction.
3. **Speed must be measured.** Hunter records detector, build, signing,
   submission, confirmation, and slot timing instead of declaring a provider
   “fast” from marketing or a single request.
4. **Unknown is not zero.** Missing fee data, ambiguous confirmation, unsupported
   quote assets, and cross-currency costs remain explicit and fail safely where
   correctness requires it.

Hunter is not tied to Telegram, a specific RPC vendor, or one execution
transport. The trading, risk, position, storage, and execution layers are being
kept interface-neutral so other front ends can be added without rewriting the
protocol path.

## Capability map

| Area | Available today |
| --- | --- |
| Protocols | Pump.fun bonding curve and LetsBonk |
| Pump.fun trading | `buy_v2` and `sell_v2`, exact account layout, SOL and configured SPL quote assets |
| Token programs | SPL Token and Token-2022-aware mint/account handling |
| Detection | Solana `logsSubscribe`, `blockSubscribe`, Yellowstone Geyser, PumpPortal, RabbitStream, Triton Riptide, and bounded Triton shred/sidecar ingress |
| Quoting | Integer reserve calculations, price impact, explicit slippage, current Pump fee state |
| Delivery | Standard Solana JSON-RPC by default; opt-in generic multi-RPC, Helius Sender/Max, Triton Jet/SWQoS, and Jito single-transaction adapters |
| Broadcast policies | `single`, `race`, `hedged`, and classified `fallback` |
| Exits | Time-based exits and take-profit/stop-loss monitoring; manual mode retains the position |
| Positions | SQLite persistence, partial exits, lifecycle history, restart reconciliation |
| Accounting | Actual transaction effects, raw cost basis, realized PnL, separate SOL network costs for SPL-quoted trades |
| Risk | Trading switch, kill switch, trade/position/exposure caps, fee caps, wallet reserve, trade-rate limit |
| Telemetry | Detection-to-land timing, provider attempts, blockhash age, fee settings, slots, confirmation progression |
| Benchmarking | Offline replay, passive multi-detector observation, read-only transport probes, and guarded opt-in economic trials |
| Maximum-performance profile | Regional multi-feed aggregation, exactly-once mint claiming, background execution caches, sender warm-up, readiness/degraded state, and capability-safe routing |
| Universal trade intents | Common trigger-neutral path for launch, tracked-wallet, manual, YOLO, managed exit, emergency, and fleet actions |
| Tracked wallets | Independent Pump.fun CREATE snipes and BUY copies, bounded decoding, durable duplicate claims, explicit sizing |
| Launch/fleet foundation | Pump.fun `create_v2`, ordered creator/participant buy plans, Jito bundle transport, fleet accounting and coordinated exits; disabled by default |
| Testing | Credential-free offline unit, protocol characterization, transaction construction, recovery, and routing tests |

### Pump.fun behavior

The normal Pump.fun path reads current curve and fee state, calculates expected
output using raw integer reserves, applies protocol and creator fees using the
vendored protocol definitions, and constructs the same verified V2 instruction
shape for SOL- and supported SPL-quoted coins. Unknown decimals and unsupported
quote assets are not guessed.

For supported event sources, complete Pump.fun `CreateEvent` data can power the
zero-read extreme-fast path. PumpPortal does not provide the same authoritative
state, so Hunter refreshes the bonding curve and mint state before building a
buy. If the refresh cannot complete within its configured budget, the trade is
skipped rather than built from guessed accounts.

### Transaction delivery

Without an `execution` section, Hunter uses the standard Solana JSON-RPC sender.
Alternative delivery is opt-in:

- **Generic RPC:** one or more standard `sendTransaction` endpoints with
  independent read, blockhash, submit, confirm, and WebSocket roles.
- **Helius Sender:** optimized submission using its documented transaction
  endpoint and a separately guarded tipped variant.
- **Jito:** normal single-transaction relay, a distinct Jito-tipped variant, the
  documented single-transaction `bundleOnly` mode, and an isolated ordered
  multi-transaction bundle adapter for launch/fleet orchestration. Bundle plans
  are separate economic components with separate signatures.

Compatible providers receive the exact same signed wire bytes and signature in
a race or hedge. A tip changes the message, so a tipped transaction is modeled
as a separate execution variant and signed once for that variant.

See [Execution providers](docs/execution-providers.md) and
[Landing metrics](docs/landing-metrics.md) for the precise semantics.

### Universal execution, tracked wallets, and launch fleets

Transaction speed no longer depends on why Hunter is trading. Launch snipes,
manual actions, YOLO continuation, tracked-wallet events, TP/SL/time/emergency
exits, and fleet exits carry a `TradeIntent` into the same exact quote, risk,
build, signing, routing, and confirmation boundaries. Source and urgency are
telemetry—not permission to bypass fee or exposure caps.

Tracked public wallets have two independent Pump.fun triggers: CREATE can snipe
immediately, while BUY can copy a successfully decoded purchase. The safe
default ignores a later copy when Hunter already has the position. Fixed and
proportional sizing use raw integers and fail closed when exact source amount or
quote denomination is unavailable.

The disabled token-launch foundation builds IDL-verified `create_v2` and
`extend_account` instructions, then plans creator and participant `buy_v2`
transactions through the existing buy factory. Current packet size makes create
and creator buy separate transactions. A Jito bundle can restore ordered atomic
delivery within its current five-transaction capacity; non-bundle modes are
explicitly partial-landing risks. See
[Universal fast execution](docs/universal-fast-execution.md),
[Tracked wallets](docs/wallet-tracking.md), and
[Token launch and wallet fleets](docs/token-launch-and-wallet-fleet.md).

For the opt-in Amsterdam-oriented infrastructure profile, see
[Maximum-performance deployment](docs/max-performance-deployment.md). The safe
example remains disabled at
[`config/examples/hunter-maximum-performance.yaml`](config/examples/hunter-maximum-performance.yaml).

### Persistence, recovery, and PnL

Positions, fills, logical executions, signatures, blockhash validity, lifecycle
transitions, and telemetry live in a versioned local SQLite database. On restart,
Hunter loads open positions, inspects pending signatures, and reconciles token
balances. A disagreement never triggers an automatic sell; the position becomes
`RECONCILIATION_REQUIRED` for review.

Realized PnL uses observed token and quote balance changes rather than a reference
price. Partial exits allocate average cost basis. SOL fees remain SOL-denominated
when a position is quoted in USDC or another SPL asset—Hunter does not invent an
FX conversion to produce a prettier number.

## Project layout

```text
bots/                 disabled example bot configurations
docs/                 architecture, providers, telemetry, risks, benchmarking
idl/                  vendored Pump.fun protocol definitions
learning-examples/    standalone verification, simulation, and manual tools
src/application/      trading façade, positions, recovery, and risk services
src/core/             Solana client, wallet, blockhash, and priority fees
src/domain/           raw amounts, quotes, execution, lifecycle, accounting
src/execution/        provider contracts, routing, telemetry, and metrics
src/monitoring/       logs, blocks, Geyser, PumpPortal, and position monitors
config/examples/      disabled infrastructure reference profiles
src/platforms/        Pump.fun and LetsBonk protocol implementations
src/storage/          SQLite schema, migrations, and repositories
src/strategies/       exit decisions without transaction submission ownership
src/trading/          current compatibility coordinator and trade services
tests/                fully offline test suite
```

Imports are rooted at `src/`. Use `from core.client import SolanaClient`, not
`from src.core.client import SolanaClient`.

## Installation

### Requirements

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/) recommended for environment management
- A Solana wallet dedicated to Hunter
- An HTTP RPC endpoint
- A matching WebSocket endpoint for logs/blocks listeners
- Geyser credentials only when using the Yellowstone listener

### 1. Clone Hunter

```bash
git clone https://github.com/dexterrbuilds/hunter.git
cd hunter
```

### 2. Install the project

```bash
uv sync
uv pip install -e .
```

The editable install makes the `hunter` and `hunter-benchmark-report` commands
available and places `src/` on the Python import path.

### 3. Create local environment configuration

```bash
cp .env.example .env
```

Edit `.env` locally:

```dotenv
SOLANA_NODE_RPC_ENDPOINT=https://your-rpc.example/?api-key=YOUR_KEY
SOLANA_NODE_WSS_ENDPOINT=wss://your-rpc.example/?api-key=YOUR_KEY
SOLANA_PRIVATE_KEY=YOUR_BASE58_SOLANA_PRIVATE_KEY

# Only required for the Geyser listener
GEYSER_ENDPOINT=your-geyser.example:443
GEYSER_API_TOKEN=YOUR_GEYSER_TOKEN
GEYSER_AUTH_TYPE=x-token
```

Never commit `.env`, keypair JSON, private YAML, authenticated endpoint URLs, or
Geyser credentials. Hunter's redactor is defense in depth—not permission to put
secrets in logs.

### 4. Choose a listener configuration

The shipped files are deliberately disabled:

| Example | Source | Pump.fun | LetsBonk |
| --- | --- | :---: | :---: |
| `bots/hunter-logs.yaml` | Solana logs | Yes | No |
| `bots/hunter-blocks.yaml` | Solana blocks | Yes | Yes |
| `bots/hunter-geyser.yaml` | Yellowstone Geyser | Yes | Yes |
| `bots/hunter-pumpportal.yaml` | PumpPortal WebSocket | Yes | Yes |

Copy the closest example and give it a unique `name`:

```bash
cp bots/hunter-geyser.yaml bots/my-hunter.yaml
```

Review every field before changing `enabled: false` to `enabled: true`.

### 5. Validate offline

No credentials and no funded wallet are required for the test suite:

```bash
uv run python -m unittest discover -s tests -v
uv run python learning-examples/verify_v2_account_layout.py
uv run python learning-examples/verify_pumpportal_buy_path.py
uv run python learning-examples/verify_extreme_fast_zero_rpc.py
uv run python learning-examples/verify_tx_status_checks.py
```

### 6. Run Hunter

```bash
uv run hunter
```

Or, after the editable install:

```bash
hunter
# equivalent entry point
uv run src/bot_runner.py
```

Hunter loads enabled YAML files from `bots/`. Configurations with
`separate_process: true` run in isolated processes. Runtime logs are written to
`logs/`, and the default position database is `data/hunter.sqlite3`; both are
ignored by Git.

## Configuration guide

### Trading

```yaml
trade:
  buy_amount: 0.01
  buy_slippage: 0.10
  sell_slippage: 0.10
  exit_strategy: tp_sl       # time_based | tp_sl | manual
  take_profit_percentage: 0.25
  stop_loss_percentage: 0.10
  max_hold_time: 300
  price_check_interval: 1
  curve_refresh_budget: 2.0
  trust_create_event: true
  extreme_fast_mode: false
```

`0.10` means 10%, not ten basis points. Non-SOL quote amounts belong in
`trade.quote_amounts` and use that mint's whole units. If no amount exists for a
quote asset, Hunter skips it rather than treating it like SOL.

`extreme_fast_mode` retains a characterized fixed-token sizing path and cannot
produce the same reserve-pinned quote as the normal Pump path. It is not the
recommended starting configuration.

### Priority fee and compute budget

```yaml
priority_fees:
  enable_dynamic: false
  enable_fixed: true
  fixed_amount: 200000
  extra_percentage: 0.0
  hard_cap: 200000
  strategy: fixed # fixed | dynamic | cached_dynamic | periodic_dynamic
  cache_ttl_seconds: 5.0
  refresh_interval_seconds: 2.0

compute_units:
  buy: 100000
  sell: 60000
  # account_data_size: 16000000
```

Priority-fee values are **micro-lamports per compute unit**. The hard cap applies
to the CU price; risk limits separately cap the resulting lamport exposure.
Pump Token-2022 trades may require a generous loaded-account data limit—do not
reduce it without simulation evidence.

`provider_estimated` is also a defined strategy boundary, but it requires an
explicit provider estimator integration and fails closed if none is supplied.

### Risk controls

Risk enforcement is off in inherited-compatible examples so existing economics
are not silently changed. New deployments should configure raw-unit limits and
enable it deliberately:

```yaml
risk:
  enforce: true
  trading_enabled: true
  emergency_kill_switch: false
  maximum_buy_raw_by_quote: {sol: 10000000, usdc: 10000000}
  maximum_position_raw_by_quote: {sol: 25000000, usdc: 25000000}
  maximum_aggregate_exposure_raw_by_quote: {sol: 50000000, usdc: 50000000}
  maximum_total_transaction_fee_lamports: 500000
  maximum_priority_fee_lamports: 250000
  minimum_wallet_reserve_lamports: 50000000
  maximum_trades_per_interval: 3
  trade_interval_seconds: 60
  reject_unknown_base_fee: true
```

These numbers are examples, not recommendations. Quote exposure values are raw
mint units: SOL uses lamports; USDC uses six-decimal raw units.

### Storage and concurrency

```yaml
storage:
  database_path: data/hunter.sqlite3

runtime:
  max_concurrent_positions: 4
```

Detection workers and position monitors are bounded independently. A held
position does not monopolize the global token queue, and duplicate mint events
are claimed before a logical buy is created.

### Execution providers

Provider routing is optional. This minimal example races the same signed
transaction through two standard RPC endpoints:

```yaml
execution:
  enabled: true
  mode: race                 # single | race | hedged | fallback
  hedge_delay_ms: 75
  maximum_blockhash_age_ms: 30000
  maximum_combined_fee_lamports: 500000
  execution_variant: standard
  jito_tip_lamports: 0
  jito_tip_account: null
  latency_budgets:            # warning-only; does not block a trade
    transaction_build_ms: 10
    signing_ms: 5
    submission_rtt_ms: 150
  providers:
    - id: rpc-primary
      kind: standard_rpc
      endpoint: ${SOLANA_NODE_RPC_ENDPOINT}
      priority: 10
      roles: [account_read, blockhash, submit, confirm]
      enabled: true
      skip_preflight: true
      max_retries: 0
    - id: rpc-secondary
      kind: standard_rpc
      endpoint: https://secondary.example.invalid/?api-key=YOUR_KEY
      priority: 20
      roles: [submit]
      enabled: true
      skip_preflight: true
      max_retries: 0
```

Use the disabled placeholder reference in
[docs/execution-providers.example.yaml](docs/execution-providers.example.yaml)
for Helius and Jito shapes. Do not copy a tip account or fee setting blindly.
Tipped execution also requires `risk.enforce: true`, fail-closed unknown base
fees, provider minimum/maximum tip bounds, and a combined fee cap. Measure each
provider from the host and region where Hunter actually runs.

## Measuring execution

Hunter keeps detection latency separate from sender latency. For each execution
it can record:

- event source, observation time, processing start, and source slots;
- quote, instruction, transaction build, and signing time;
- blockhash source, age, source slot, and last-valid height;
- every provider attempt, sanitized endpoint ID, bytes sent, and acknowledgement;
- first observed commitment, landed slot, and detection/submission slot deltas;
- base fee, CU priority fee, Jito tip, rent, and other known costs separately.

Summarize persisted provider evidence with:

```bash
uv run hunter-benchmark-report data/hunter.sqlite3
```

The report includes sample count, median/p90 submission RTT, landing latency,
slots-to-land, same-slot/+1/+2-or-later percentages, failures, ambiguous outcomes,
and known fee cost. It does not rank providers until the minimum evidence count
is met.

“Block 0” has a strict meaning in Hunter: the transaction landed in the same
authoritative launch slot as the token creation. If a source does not provide an
authoritative launch slot—PumpPortal currently does not—Hunter reports detection
and landing data without manufacturing a block number.

Live benchmark submission is disabled by default and isolated from normal bot
startup. Passive detection and read-only transport commands can gather regional
evidence without buying. A tiny economic trial requires an explicit mint, raw
amount, independent hard caps, active risk enforcement, an exact acknowledgement,
and the `--allow-live` flag. See [Controlled live benchmark](docs/live-benchmark.md).

## What is not implemented

To keep the project honest, these are not current features:

- Telegram control or notifications
- Web UI, public API, or interactive CLI trading
- A production-startup or user-interface composition for tracked-wallet and
  launch/fleet services (the tested services and disabled config foundation now
  exist)
- Automatic token-selection strategies beyond the existing filters
- Production-proven provider ranking
- Production-proven multi-wallet launch/fleet operation
- Hardware or remote wallet signing
- Automatic FX conversion of SOL fees into SPL quote PnL

## Roadmap

### Near term

- Benchmark detection and landing performance from real deployment regions
  using explicit, controlled opt-in procedures.
- Finish the incremental removal of the `UniversalTrader` compatibility
  coordinator without changing verified Pump.fun transaction bytes.
- Complete integer/raw-unit migration for remaining LetsBonk and extreme-fast
  monetary paths.
- Strengthen interrupted-buy recovery and portfolio loss/drawdown controls.
- Compose tracked-wallet monitoring and launch/fleet services behind an explicit
  operator interface only after controlled live validation.

### Later

- A personal Telegram interface for balances, buys, sells, positions, PnL,
  settings, transaction history, and notifications.
- Strategy-managed take profit, stop loss, trailing stop, and filters backed by
  persisted positions—not chat state.
- Additional interfaces such as a local CLI, web dashboard, or private API that
  call the same trading engine.
- Expand tracked-wallet venue coverage and policy controls only after execution
  idempotency, risk, and reconciliation have been proven under real conditions.

No provider or delivery method will be labeled “fastest” without Hunter's own
measurements from the intended deployment environment.

## Safe development rules

- Run the offline suite before and after protocol or transaction changes.
- Never use a funded wallet to validate code.
- Never run `manual_*`, `mint_and_buy*`, or cleanup scripts as tests; several of
  them submit real transactions.
- `simulate_*` and `verify_*` learning examples are the no-funds verification
  tools. Read every script's module docstring before running it.
- Do not hand-edit vendored IDLs. Refresh them from their recorded upstream
  source and re-run the protocol characterization suite.
- Lint only changed files; the inherited tree has a known legacy lint baseline.

## Documentation

| Document | Purpose |
| --- | --- |
| [Architecture](docs/architecture.md) | Module boundaries, amounts, quoting, accounting, persistence, lifecycle |
| [Execution interfaces](docs/execution-interfaces.md) | Account, blockhash, builder, signer, submitter, confirmation contracts |
| [Execution providers](docs/execution-providers.md) | Standard RPC, Helius, Jito, routing, identity, connection reuse |
| [Execution telemetry](docs/execution-telemetry.md) | Durable schema, monotonic timing, provider attempts, fee fields |
| [Landing metrics](docs/landing-metrics.md) | Slot definitions, same-slot reporting, detection ambiguity |
| [Benchmarking](docs/benchmarking.md) | Offline replay, reports, and explicit live opt-in rules |
| [Controlled live benchmark](docs/live-benchmark.md) | Passive detection, transport probes, guarded tiny trades, exports, and interpretation |
| [Universal fast execution](docs/universal-fast-execution.md) | Trigger-neutral intents, shared execution, urgency, and zero-read rules |
| [Tracked wallets](docs/wallet-tracking.md) | Independent CREATE/BUY triggers, decoding, sizing, duplicates, and recovery |
| [Token launch and wallet fleets](docs/token-launch-and-wallet-fleet.md) | Pump.fun launch plans, bundles, risks, fleet accounting, exits, and recovery |
| [Known risks](docs/known-risks.md) | Unresolved funds, accounting, recovery, protocol, and throughput risks |
| [Upstream provenance](UPSTREAM.md) | Imported source snapshot, derivative-work notice, IDL checksums |

## License


Licensed under the [Apache License 2.0](LICENSE).

---

<div align="center">

### Build carefully. Measure everything.

[**Telegram — @dexterrbuilds**](https://t.me/dexterrbuilds) ·
[**X — @dexterrbuilds**](https://x.com/dexterrbuilds) ·
[**GitHub — Hunter**](https://github.com/dexterrbuilds/hunter)

</div>
