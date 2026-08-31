# Hunter runtime composition

Hunter has one application composition boundary: `HunterApplication`. Normal
startup constructs the established trader once, then hands its owned services
to the application runtime. The composition layer contains no Pump.fun math,
address derivation, instruction encoding, signing algorithm, or
provider-specific transport logic.

```text
bot YAML
   |
   v
HunterApplication
   |-- SQLite and recovery
   |-- wallet and RiskService
   |-- Solana reads and execution providers
   |-- blockhash and priority-fee lifecycle
   |-- managed buy/sell compatibility path
   |-- position monitors
   |-- configured listener / earliest-event aggregator
   |-- optional tracked-wallet source and processor
   |-- optional launch/fleet application services
   |-- bounded application events
   `-- task supervision and typed runtime status
```

## Ownership

Each bot instance owns strategy/filter state, positions, SQLite, wallet, risk
limits, listeners, token workers, and position monitors. Multiple enabled YAML
configurations can run concurrently in one process; `separate_process: true`
retains process isolation.

Provider sessions, fee refreshers, blockhash state, and telemetry writers remain
owned by that bot's `SolanaClient`/`UniversalTrader`; the composition root does
not create a second copy. Cross-bot connection pooling is deferred because bots
may use different credentials, rate limits, wallets, and databases. Mutable
trading state is never shared.

## Feature gates

Disabled wallet tracking creates no wallet subscriptions or decoder workers.
Disabled launch/fleet configuration creates no signers, bundle service, or fleet
scheduler. Enabled launch/fleet configuration must be supplied a complete
signer/orchestration factory by the embedding application; Hunter fails before
listener startup instead of constructing a partial economic service. Shipped
examples keep all economic features disabled.

Tracked-wallet mode is composed into normal startup. It validates addresses,
loads the Pump IDL, starts a bounded decoder pool, durably claims CREATE or
successful BUY observations, applies copy sizing and duplicate policy, then
passes a typed `TradeIntent` through the runtime gate and managed execution
boundary. CREATE carries authoritative `TokenInfo`; BUY uses the established
curve refresh. Failed transactions, sells, transfers, and unrelated
instructions remain ignored.

## Framework-neutral controls

The application exposes typed methods for runtime status, trading enable and
disable, kill-switch activation, positions, tracked-wallet configuration,
manual intents, launch preview/submission, and fleet exits. These are not HTTP
or Telegram handlers and expose no raw provider client or signer material.

`ApplicationEventBus` is bounded and asynchronous. Notification consumers never
block transaction execution. Queue drops and consumer failures are counted.
SQLite—not the event bus—remains authoritative for economic state.

## Backpressure and reconnects

Detection, tracked-wallet decoding, position monitoring, provider telemetry,
and application events use bounded queues. Portable tracked-wallet monitoring
uses one subscription per address because core Solana `logsSubscribe` accepts
one `mentions` address per subscription. It reconnects with bounded exponential
backoff and jitter, resets after a successful handshake, and relies on durable
claims to suppress replay duplicates. Geyser deployments may feed the same
normalized observation model more efficiently.

## Read-only operation

Readiness and trading permission are separate. Hunter can recover, warm
providers, run listeners, collect telemetry, and report status while trading is
disabled. The runtime gate and RiskService reject every exposure-increasing
entry while preserving managed defensive exits. Activating the kill switch has
the same exposure-halt semantics: it blocks buys, launch snipes, tracked-wallet
entries, YOLO entries, token launches, launch bundles, and additional fleet
exposure, but does not block an owned-position manual sell, TP, SL, timed exit,
emergency exit, or eligible fleet exit.

An exit source is not an unrestricted sell bypass. It must carry an existing
managed position identity and still passes ownership, signer, risk, fee,
idempotency, venue, and execution checks. The runtime revalidates immediately
before managed submission, so an entry prepared before a halt cannot cross the
boundary afterward. Prepared-but-unsent launch components are revalidated too.
Already-submitted transactions remain under confirmation/recovery; Hunter does
not attempt cancellation or create a replacement merely because controls
changed.

The kill switch never liquidates automatically and does not override
`marry_mode`. An emergency sell remains a separate explicit managed action.
`RuntimeStatus` reports application readiness, the trading and kill-switch
states, whether entries are allowed, and whether defensive exits are available.

No broad simulator was added. Offline fakes exercise the real composition root;
live execution remains governed by existing explicit benchmark/trading config.
