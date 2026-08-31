# Production readiness

Hunter is independently maintained, pre-production software. “Implemented,”
“offline tested,” and “runtime composed” do not mean “live validated.”

## Runtime composed and offline tested

- versioned SQLite recovery and reconciliation barrier;
- exact Pump.fun curve quotes and transaction-effect accounting;
- standard RPC and opt-in provider-neutral execution routing;
- blockhash, fee, telemetry, confirmation, and idempotency lifecycle;
- bounded token and position concurrency;
- earliest-event aggregation for configured launch detectors;
- normal-runtime tracked-wallet CREATE and successful BUY dispatch;
- framework-neutral status, control, launch-preview, and fleet boundaries;
- bounded application events and observable task failure;
- concurrent multi-bot startup with bot-scoped mutable state;
- offline composition tests that use no provider connection.

## Requires deployment-specific composition or validation

- token launch and wallet fleet need an explicit signer registry,
  buy-component factory, bundle provider, balance reader, and landing accounting;
- RabbitStream, Riptide, Helius, Triton Jet, Jito, and paid RPC behavior must be
  measured from the operator's region;
- raw Triton shreds require a reviewed provider SDK or sidecar;
- launch/fleet workflows have not been mainnet validated;
- provider capabilities and APIs may change after release.

## Before a funded deployment

1. Use a dedicated, minimally funded wallet and private bot YAML.
2. Enable and calibrate every risk, reserve, and fee cap.
3. Start trading-disabled; inspect `runtime_status()` and passive telemetry.
4. Verify database durability, permissions, clock synchronization, regional
   endpoints, and restart behavior on the target host.
5. Use the controlled live benchmark with explicit authorization and tiny caps.
6. Validate current Pump definitions and offline instruction fixtures.
7. Exercise termination/restart with a fake or isolated ambiguous execution.
8. Keep logs and exports free of authenticated endpoints and keys.

No provider is universally fastest. Detection latency, construction time,
submission RTT, landing slots, failure rate, and cost must be measured together
from Hunter's deployment region.
