# Safe benchmarking

Hunter separates benchmarks into three categories. Offline replay stays entirely
local. Transport benchmarks make read-only RPC calls. Economic benchmarks are
isolated, explicitly authorized Pump.fun buys and optional sells guarded by the
normal risk engine and a second set of benchmark-only limits.

Automated tests only use fake providers. They never submit to mainnet or devnet.

## Commands

```bash
# Passive launch observation; no wallet and no transaction
hunter-benchmark-detection observer.yaml --duration 300 \
  --database data/hunter-benchmarks.sqlite3 --region-label frankfurt-vps

# Read-only health/blockhash/account/status/fee-estimator RTT
hunter-benchmark-transport provider-config.yaml --iterations 10 \
  --database data/hunter-benchmarks.sqlite3 --region-label frankfurt-vps

# Controlled economic trial; refuses without all authorization conditions
hunter-benchmark-live bots/private-benchmark.yaml --route rpc-primary \
  --database data/hunter-benchmarks.sqlite3 --allow-live

# Credential-free human summary with optional JSON/CSV export
hunter-live-benchmark-report data/hunter-benchmarks.sqlite3
hunter-live-benchmark-report data/hunter-benchmarks.sqlite3 \
  --format csv --export benchmark-summary.csv
```

The older `hunter-benchmark-report` command continues to summarize ordinary
execution telemetry in the position database.

## What a report means

Detection sources are correlated by creation transaction signature when a
listener supplies one, otherwise by mint. Each source is compared with the
earliest Hunter observation of that same creation. Provider attempts retain
failures and are split by cold, warm, and reconnected connection state.

In a race, all compatible providers receive identical signed bytes. Landing the
shared signature cannot prove which provider caused inclusion, so per-provider
acknowledgement RTT and shared landing evidence must not be confused with causal
attribution. A tipped variant changes the message and is a separate economic
trial.

Provider rows are not ranking-eligible until the minimum sample count is met.
Even then, compare like-for-like detector, route, fee, commitment, wallet, token,
and region conditions. No provider is universally fastest.

See [Controlled live benchmark](live-benchmark.md) for the authorization and
financial-safety procedure.
