# Safe benchmarking

Automated Hunter benchmarks are offline. `OfflineReplayBenchmark` can replay
recorded detector inputs through pure construction/parsing callbacks, and
`benchmark_sync` measures construction or signing callables. Read, blockhash,
status, and simulation probes can be built on the provider interfaces, but no
automated test submits a transaction.

The safety model defaults to:

```yaml
benchmark:
  allow_live_submission: false
```

Any future live or non-economic submission command must call the explicit
opt-in guard. Milestone 3 provides no automatic live-send benchmark, and
validation performs no mainnet or devnet submission.

## Provider report

Generate a JSON summary from Hunter's local telemetry database:

```bash
hunter-benchmark-report data/hunter.sqlite3
```

The report groups sanitized provider/endpoint identities and includes sample
count, median/p90 submit RTT, median/p90 submit-to-land, median slots-to-land,
same-slot/+1/+2-or-later percentages, failure rate, ambiguous outcome rate,
and estimated average known SOL fee. A provider is only marked
`ranking_eligible` after the configured minimum sample count (20 by default).
The tool does not declare a winner; samples must also use comparable detection,
transaction, fee, and confirmation policies.

In a race, submit-to-land is measured from each adapter attempt's monotonic send
time, but landing the shared signature cannot prove which transport caused
inclusion. Slot and outcome evidence therefore describes the shared economic
execution and must not be presented as causal provider attribution.

Safe live evaluation, if added later, should use a separately funded wallet,
an explicitly non-economic transaction, strict fee caps, a known cluster, and
manual invocation. It must never reuse a production trading wallet merely for
benchmark convenience.
