# Terminal operator interface

Hunter's terminal interface is an optional, local adapter over
`HunterApplication`. It displays typed runtime state and invokes the same runtime
control methods used by other interfaces. It is not a second trading engine and
does not construct transactions, contact providers directly, or own Hunter's
process lifecycle.

## Enable it

Textual is installed with Hunter (`textual>=8.2,<9`), but no Textual application
or terminal task is created unless the bot config explicitly enables it:

```yaml
terminal_ui:
  enabled: true
  refresh_interval_seconds: 1.0
  balance_refresh_seconds: 30.0
  event_queue_size: 256
  event_history_size: 100
  position_page_size: 25
  execution_page_size: 50
  telemetry_sample_size: 200
```

The complete safe reference is
[`config/examples/hunter-terminal-ui.yaml`](../config/examples/hunter-terminal-ui.yaml).
The interface needs no token, password, network listener, or separate database.
It starts as an optional supervised control interface after Hunter crosses its
recovery and infrastructure warm-up barrier. A startup/rendering failure is
reported as a degraded optional component and does not gate detection, trading,
or managed exits.

Only one full-screen TUI may be enabled among bot configurations sharing a
process. Use `separate_process: true` when separate bot instances each need a
terminal. Each interface is bot-scoped; it never combines mutable state across
bot instances.

## Navigation

The left navigation contains these views:

1. Overview
2. Runtime and recovery
3. Detection feeds
4. Providers and routing
5. Positions
6. Position details
7. Execution history
8. Transaction lookup
9. Wallets and balances
10. Tracked wallets
11. Wallet fleet
12. Token launches
13. Risk configuration
14. Recent events and errors
15. Latency and landing metrics
16. Settings and diagnostics

The position and execution views use bounded pages. Wallet balance reads have a
separate, longer refresh interval. Rendering never performs per-row RPC calls.
Missing measurements say `unavailable`, `unmeasured`, `stale`, or
`insufficient`; they are never rendered as zero. p50 requires at least five
samples, p95 at least twenty, and p99 at least one hundred.

Keyboard controls:

| Key | Action |
| --- | --- |
| Arrow keys / Enter | Navigate views |
| `r` | Request a coalesced refresh |
| `n` / `p` | Next / previous bounded position or execution page |
| `e` | Stage enable-trading confirmation |
| `d` | Stage disable-new-exposure confirmation |
| `k` | Stage kill-switch activation confirmation |
| `c` | Inspect/confirm kill-switch clearing request (currently fails closed) |
| `q` | Close the TUI only |

Closing the TUI does not activate the kill switch, liquidate positions, cancel
submitted transactions, or shut down Hunter. The process runtime remains the
owner of Hunter shutdown.

## Safe controls

Every consequential action uses a short-lived opaque server-side nonce and a
modal describing what the action does. Confirmation atomically consumes the
nonce and re-reads runtime state immediately before calling the application API.
A stale selection therefore cannot bypass a newly activated kill switch or a
changed readiness state.

- **Enable trading** permits new exposure only if recovery is complete and the
  kill switch is inactive. Risk and final managed-execution authorization remain
  authoritative.
- **Disable trading** blocks new exposure while listeners, recovery, telemetry,
  and eligible managed defensive exits continue.
- **Activate kill switch** blocks new exposure. It does not liquidate, cancel an
  already-submitted Solana transaction, or block eligible defensive exits.
- **Clear kill switch** is deliberately unsupported because
  `HunterApplication` has no safe clear API. The TUI requires confirmation and
  then fails closed instead of mutating risk state directly.

M5 does not expose manual buy, manual sell, token launch, fleet exit, arbitrary
risk editing, or ambiguous-transaction resubmission. The repository has some
application-level economic APIs, but adding a second terminal confirmation and
preview workflow was intentionally outside this read-mostly milestone.

## Events and responsiveness

Application events enter a bounded, non-blocking terminal buffer. Overflow
drops the oldest terminal-only presentation event and increments a visible
counter; economic truth remains in SQLite. Repeated refresh requests coalesce
into one read cycle. SQLite queries run away from the renderer, telemetry and
execution history are bounded, and balance reads are cached for the configured
interval.

The TUI imports no provider, signer, transaction builder, submission, or
confirmation implementation. There is no UI await in detection, quote, build,
sign, route, send, or defensive-exit code.

## Data and security

- The interface is intended for a trusted local terminal. Running it over SSH
  is supported only to the extent the operator trusts and secures that SSH
  session and host.
- It opens no TCP/HTTP listener and terminal visibility is not authorization to
  bypass application controls.
- Private keys, seeds, Telegram tokens, authenticated endpoints, and configured
  secrets are not part of terminal snapshots.
- Public keys and transaction signatures remain visible because they are useful
  for inspection; actions never use visually truncated identifiers.
- Errors and event details pass through Hunter's credential redactor before
  rendering.

## Supported and unavailable data

Runtime readiness, recovery, listener/provider state, risk settings, managed
positions, bounded execution history, tracked-wallet configuration, cached
wallet balance, persisted execution telemetry, and startup timings use existing
typed APIs.

Current valuation/unrealized PnL is shown as unavailable because the TUI does
not create a new live price-query path. Fleet detail requires an existing plan
ID, launch history lacks a bounded application query, and arbitrary transaction
lookup has no dedicated bounded query; those screens state the limitation rather
than reaching through the application boundary.

## Troubleshooting

- **TUI says degraded:** inspect the data-error and runtime screens. Provider or
  persistence failures do not stop the renderer; stale sections remain clearly
  qualified.
- **No balance yet:** balance refresh is independently rate-limited and may be
  unavailable while the read provider is degraded.
- **p95/p99 unavailable:** collect enough persisted measurements; Hunter will
  not extrapolate from a tiny sample.
- **Second TUI rejected:** enable `separate_process: true` for that bot or leave
  only one full-screen interface enabled in the shared process.
- **Terminal formatting is damaged:** resize or close with `q`. A rendering
  failure remains isolated from Hunter's trading task.

## Known limitations

The interface is offline-tested but has not been exercised against a live-funded
runtime. It is not a replacement for durable monitoring or alerting. Terminal
access is local-host access, no role model exists, and kill-switch clearing is
intentionally unavailable. See [Known risks](known-risks.md).
