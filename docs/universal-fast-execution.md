# Universal fast execution

Hunter treats the trigger as the reason for a trade, not as an execution
backend. A launch observation, tracked-wallet event, manual request, YOLO
continuation, TP/SL decision, timed exit, emergency request, and wallet-fleet
exit all become a typed `TradeIntent` before they reach the existing buy or sell
application service.

```text
trigger -> TradeIntent -> strategy/filter checks -> risk -> quote/plan
        -> build -> sign once -> capability-safe routing -> confirmation
```

The intent carries a stable ID, action, source, wallet reference, exact raw
amount, quote mint, slippage, urgency, source signature/slot, and non-secret
metadata. Source and urgency are observability and scheduling fields. They do
not select a provider or override fee and risk caps.

## Paths using the common pipeline

- normal Pump.fun launch buys and YOLO continuation buys;
- tracked-wallet CREATE snipes and BUY copies;
- manual buys and sells through `TradingEngine`;
- take-profit, stop-loss, timed, and emergency sells;
- wallet-fleet buy/sell components through their bounded orchestrators.

`UniversalTrader` remains a compatibility coordinator while the incremental
split continues. Its active paths call the same `PlatformAwareBuyer` and
`PlatformAwareSeller`, which in turn use the same configured `SolanaClient`
execution coordinator and Milestone 3.6 provider stack. A few standalone
learning-example harnesses retain their legacy direct calls by design; they are
not a second production performance stack.

`HunterApplication` is now the runtime gate above that coordinator. Recovery
and exposure authorization are checked at the application boundary and again
at the final managed execution boundary. Trading disable and kill-switch state
block entries while owned-position defensive exits remain available.
Dependency resolution and configuration parsing are not repeated on the
transaction hot path.

## Fast does not mean guessed

Universal fast execution removes unnecessary synchronous work. It does not
promise zero RPC reads for every trade. A complete, authoritative Pump.fun
`CreateEvent` can retain the characterized zero-read launch-buy path. PumpPortal
and incomplete events still require the minimum authoritative curve/mint
refresh and fail closed if it cannot be obtained. Sells still need current
dynamic reserve state for an exact minimum output.

`extreme_fast_mode` therefore remains a trade-behavior option with its existing
fixed-token sizing semantics. It is not the switch that makes other trade
sources fast.

## Urgency and telemetry

Default urgency is `CRITICAL` for emergency and stop-loss exits, `HIGH` for
launch/copy and managed exits, and `NORMAL` for ordinary manual work. Absolute
priority-fee, tip, transaction-cost, balance, exposure, and rate guards still
apply.

Every execution plan can retain intent receipt, source, urgency, risk and quote
timing. The existing execution telemetry adds build, sign, provider dispatch,
acknowledgement, commitment, landed slot, fee, and error data. Benchmark exports
can group these stable categories:

`LAUNCH_SNIPE`, `TRACKED_WALLET_CREATE`, `TRACKED_WALLET_BUY`, `MANUAL`, `TP`,
`SL`, `TIME_EXIT`, `TOKEN_LAUNCH`, and `FLEET_EXIT`.

## Preserved behavior

`marry_mode` still means buy but never automatically sell. Manual and emergency
semantics remain explicit. `yolo_mode` still controls continuous trading; it
does not bypass idempotency, risk, duplicate policy, or wallet reserves. Cleanup
policies remain outside the urgent submission path. PumpSwap migration awareness
and LetsBonk continue through their existing venue-specific implementations.
