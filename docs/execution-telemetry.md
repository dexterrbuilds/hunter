# Execution telemetry schema

Milestone 3 instruments detection, the active standard JSON-RPC path, and every
opt-in provider submission attempt with the schema in
`src/execution/telemetry.py`. Completed snapshots are queued for asynchronous
SQLite persistence after confirmation/inspection rather than adding disk I/O
to submission.

## Clock rules

Wall-clock timestamps are timezone-aware UTC values used for correlation with
slots, RPC logs, and external systems. Latencies are calculated only from
monotonic nanosecond readings captured by the same process. Wall-clock values
must never be subtracted to measure execution latency.

## Lifecycle fields

| Stage | UTC timestamp | Monotonic timestamp |
|---|---:|---:|
| Token detected | `detected_at` | `detected_mono_ns` |
| Trade requested | `trade_requested_at` | `trade_requested_mono_ns` |
| Build start | `build_started_at` | `build_started_mono_ns` |
| Build complete | `build_completed_at` | `build_completed_mono_ns` |
| Signing start | `signing_started_at` | `signing_started_mono_ns` |
| Signing complete | `signing_completed_at` | `signing_completed_mono_ns` |
| Submission start | `submission_started_at` | `submission_started_mono_ns` |
| RPC response | `rpc_responded_at` | `rpc_responded_mono_ns` |
| Signature receipt | `signature_received_at` | `signature_received_mono_ns` |
| Processed | `processed_at` | `processed_mono_ns` |
| Confirmed | `confirmed_at` | `confirmed_mono_ns` |
| Finalized | `finalized_at` | `finalized_mono_ns` |

All fields are optional until that stage occurs. A record can therefore
represent failures during construction, signing, transport, or confirmation.

## Context and outcome fields

- `execution_id`: local correlation identifier
- `provider_id`: logical provider/adapter name
- `endpoint_id`: credential-free endpoint identifier
- `logical_trade_id` and `execution_variant`
- detector source, event/transaction/launch slots, and processing timestamps
- `transaction_signature`
- `blockhash`
- blockhash source provider, source slot, and age at submission
- `last_valid_block_height`
- `submitted_slot`
- `landed_slot`
- `compute_unit_limit`
- `compute_unit_price_micro_lamports`
- `priority_fee_lamports`
- fee estimate source, age, and estimation latency
- separate base network fee, CU priority fee, Jito tip, rent, and other costs
- `transaction_size_bytes`
- `attributes.serialization_ms` for the signed wire serialization step
- `error_classification`
- `error_code`: sanitized provider or program code
- `error_detail`: sanitized bounded detail for diagnosis

`endpoint_id` is a scheme/host label plus a short one-way fingerprint. It must
not contain URL userinfo, query parameters, path API keys, or registered secret
values.

## Derived measurements

Consumers may derive build, signing, RPC-response, signature,
landing, processed, confirmed, finalized, and end-to-end latency from monotonic
stage pairs. Missing stages produce no derived latency rather than a guessed
value.

`provider_attempts` records bytes leaving Hunter, acknowledgement type/time,
provider response class, Hunter-side HTTP-session creation/reuse generation,
and sanitized diagnostics for each transport. Session reuse does not claim
visibility into a provider proxy's underlying TCP socket. See
[landing-metrics.md](landing-metrics.md) for exact slot definitions and
[benchmarking.md](benchmarking.md) for the comparison report.

Provider comparisons must use equivalent submission and confirmation policies
and report distributions, sample counts, failure rates, and observation period.
The schema alone is not evidence that one provider is faster than another.
