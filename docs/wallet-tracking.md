# Tracked wallets

Hunter's tracked-wallet service watches operator-supplied public addresses. A
label is local metadata, not a claim about who owns or controls an address.

Two event types are independent:

```text
tracked CREATE -> identify Pump.fun mint -> filters/risk -> launch snipe
tracked BUY    -> decode successful Pump.fun buy -> copy sizing -> risk -> buy
```

A CREATE is eligible immediately; Hunter does not wait for the same wallet to
buy. A later BUY is a separate durable event. The default duplicate policy,
`ignore_existing_position`, prevents that overlap from accidentally adding a
second position. `allow_additional_copy` and `aggregate_position` are explicit
policies for later composition with position rules.

## Detection and decoding

The portable adapter uses processed `logsSubscribe` filtered by each configured
wallet. The decoder accepts authoritative Pump.fun `CreateEvent` and
`TradeEvent` data and rejects failed transactions, sells, transfers, setup
transactions, unrelated programs, and events whose `user` is not tracked.
Milestone 3.6 Geyser, Riptide, RabbitStream, or reviewed shred-sidecar adapters
can emit the same `WalletTransactionObservation` model; no polling loop is
required.

The bounded processor has a fixed queue and worker count. Durable event claims
prevent duplicate source signatures from creating duplicate intents across a
restart. SQLite claim/completion work is moved to worker threads so it does not
block the async listener loop.

Milestone 3.8 composes this path into normal `HunterApplication` startup when
`wallet_tracking.enabled` is true. Subscription handshake, receiving,
degradation, reconnect, and stop states appear in the typed runtime snapshot.
Reconnect uses bounded exponential backoff with jitter; durable claims remain
the replay authority.

## Sizing

- `fixed`: use the exact configured raw/decimal quote amount;
- `percentage_of_source`: multiply an authoritatively decoded source quote
  amount using integer basis points and round down;
- `percentage_of_wallet`: multiply an explicit current wallet quote balance and
  round down.

Percentage-of-source fails closed when the source amount is unavailable. Fixed
sizing fails closed if its quote mint differs from the observed trade. Unknown
decimals are never defaulted. The normal RiskService still owns max buy,
position, exposure, wallet reserve, fee, and rate checks.

## Safe configuration

Wallet tracking defaults off. See
[`config/examples/hunter-wallet-orchestration.yaml`](../config/examples/hunter-wallet-orchestration.yaml).
The example uses placeholders, tiny illustrative amounts, a bounded queue, and
no usable credential.

## Telemetry and recovery

Persisted tracked events contain the public tracked address, optional label,
CREATE/BUY type, source signature and slot, mint, observation/decode time,
intent ID, and completion state. They contain no private key or authenticated
endpoint. A claimed event is never blindly replayed after restart; failed or
ambiguous execution remains available for inspection under its stable intent
identity.
