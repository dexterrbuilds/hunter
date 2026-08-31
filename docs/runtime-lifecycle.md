# Runtime lifecycle

Hunter treats startup, economic readiness, and shutdown as observable state.

```text
CREATED
   |
CONFIG_VALIDATED
   |
PERSISTENCE_READY
   |
RECOVERY_RUNNING --------> FAILED
   |
INFRASTRUCTURE_WARMING ---> NOT_READY / DEGRADED
   |
INFRASTRUCTURE_READY
   |
SERVICES_STARTING
   |
READY <-------------------> DEGRADED / NOT_READY
   |
SHUTTING_DOWN
   |
STOPPED
```

## Recovery barrier

The runtime starts telemetry persistence, loads positions, inspects ambiguous
sell identities, reconciles wallet balances, and queues eligible monitors
before opening the recovery barrier. Monitor workers are stopped during this
phase. A sell that is accepted but not observed is never replaced merely
because Hunter restarted.

After recovery, Hunter warms priority-fee state, RPC/blockhash state, sender
sessions, and maximum-performance readiness. Only then are monitor workers and
event producers activated. Runtime-generated intents require:

- completed recovery;
- `READY`, or explicitly permitted `DEGRADED`, application state;
- enabled runtime trading and an inactive kill switch for new exposure;
- an existing managed position identity for a defensive exit;
- approval from the existing RiskService for the exact plan and fees.

Trading disable and kill-switch activation halt new exposure, not recovery or
position defense. Managed manual sells, TP, SL, timed exits, emergency exits,
and eligible fleet exits remain authorized while normal ownership, risk, fee,
idempotency, signer, and venue checks continue. Source/action classification is
typed and exhaustive; an entry source cannot be relabeled as a sell bypass.
Authorization is repeated at the managed execution boundary to close the race
between intent creation and submission.

## Detection readiness

Listeners report `CONFIGURED`, `CONNECTING`, `CONNECTED`, `RECEIVING`,
`DEGRADED`, `FAILED`, or `STOPPED` where adapters expose that evidence. The
primary listener moves to `RECEIVING` on its first decoded event before that
event can trade. Tracked-wallet subscriptions report their handshake and
received observations. Object construction alone is not reported as traffic.

RabbitStream, Riptide, Yellowstone, logsSubscribe, blockSubscribe, and
PumpPortal remain in the established listener factory. Aggregate mode retains
one earliest-event economic claim per mint. Raw Triton shreds remain fail-closed
without a reviewed SDK/sidecar reconstructor.

## Task failure policy

Top-level tasks are classified critical, restartable, or optional. A critical
detector crash moves Hunter out of economic readiness and emits an application
event. Reconnecting adapters own bounded retry loops. Position monitors,
blockhash/fee refreshers, provider health, and telemetry retain their existing
component ownership, preventing duplicate worker lifecycles.

## Shutdown

Shutdown is idempotent and bounded:

1. Disable new exposure while retaining already-submitted recovery state.
2. Enter `SHUTTING_DOWN`.
3. Cancel and join supervised detection.
4. Stop tracked-wallet producers and drain decoder workers.
5. Stop monitors and background estimators.
6. Flush persistence and queued telemetry.
7. Close provider/RPC sessions and SQLite.
8. Enter `STOPPED`.

A submitted transaction cannot be cancelled. Its logical execution ID,
signature, blockhash, and validity state remain available to recovery; shutdown
never authorizes a replacement signature.

The console runtime registers one process-level coordinator for `SIGINT` and
`SIGTERM`. It shuts down every active in-process bot through the same sequence.
Platforms without asyncio signal-handler support retain normal `asyncio.run`
cancellation behavior. A process manager should still allow the shutdown grace
period before forcing termination.
