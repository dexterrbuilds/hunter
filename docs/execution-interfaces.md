# Future execution interfaces

This document defines the boundary for a later execution-layer refactor. It is
a design contract only: Milestone 1 does not replace or route around the current
`core.client.SolanaClient` implementation.

## Principles

- Pump.fun instruction construction must not know how a transaction is delivered.
- A signer must not know which RPC provider is used.
- Submission and confirmation are separate operations with separate results.
- Provider-specific errors are normalized without discarding their raw details.
- Endpoint identifiers are stable but never contain credentials.
- Every implementation must be testable with deterministic offline fakes.

## Contracts

### Account reader

Reads one or more accounts at a stated commitment and returns the response slot,
owner, lamports, and raw data. Batched reads must preserve a single response
context. A later implementation may expose minimum-context-slot behavior.

### Blockhash provider

Returns a blockhash together with its `last_valid_block_height`, source provider,
retrieval slot, retrieval time, and cache age. Consumers must be able to reject
an expired or stale value.

### Priority-fee estimator

Accepts the writable account set, compute-unit limit, and urgency policy. It
returns a compute-unit price in micro-lamports, the estimated maximum total
priority fee, source, observation slot, and measurement latency.

### Transaction builder

Accepts ordered protocol instructions, compute-budget settings, fee payer, and
blockhash context. It returns unsigned canonical bytes plus the build metadata
needed for testing: instruction order, size, and required signers.

### Signer

Accepts canonical message bytes and returns signatures without exposing secret
material. Local keypairs, hardware devices, and remote signers should implement
the same contract. Logs may identify a public key but never signer secrets.

### Transaction submitter

Accepts one signed transaction and submission options. It returns the provider
response, signature, response timestamp, and any provider-observed submission
slot. Submission does not imply confirmation or on-chain success.

### Confirmation service

Observes a signature independently of submission. It records processed,
confirmed, and optionally finalized states; landed slot; `meta.err`; blockhash
expiry; and ambiguous outcomes such as accepted-but-not-observed.

### Execution telemetry

Receives immutable lifecycle events or snapshots. The telemetry sink must not
block transaction construction or submission and must redact endpoints before
persistence. The schema is specified in
[execution-telemetry.md](execution-telemetry.md).

## Intended dependency direction

```text
trading service
    │
protocol instruction planner
    │
transaction builder ── signer
    │
transaction submitter
    │
confirmation service

account reader, blockhash provider, fee estimator, and telemetry are injected
ports shared by the orchestration layer.
```

The first adapter in a later milestone will wrap the current standard Solana
RPC behavior so that it remains the benchmark baseline. Alternative delivery
methods will only be added after equivalent tests and measurements exist.
