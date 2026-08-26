# Detection and landing metrics

Hunter uses UTC timestamps for external correlation and monotonic nanoseconds
for elapsed time. It never subtracts wall-clock timestamps to report latency.

## Slot definitions

- **Launch slot:** the slot containing the authoritative token-creation
  transaction, only when the detector provides it.
- **Detection slot:** the source slot attached to the event Hunter observed.
  For logs, blocks, and Yellowstone creation-transaction events this is also
  the transaction/launch slot. PumpPortal does not provide an authoritative
  slot, so it remains unknown.
- **Submission slot:** the first processed slot Hunter observes asynchronously
  after provider acknowledgement. This is an observation bound, not proof of
  the exact instant a provider ingested bytes.
- **Landed slot:** the slot returned by authoritative transaction metadata.

Hunter reports `same detection slot`, `+1`, `+2`, or `+N` from detection slot
to landed slot. It may call a result **block 0** only when launch slot is known
and landed slot equals launch slot. **Block 1** means landed at launch slot + 1;
**block N** means the exact non-negative slot delta. Detection-relative same
slot is reported separately and is not renamed block 0 when launch is unknown.

## Latencies

The derived `LandingMetrics` fields are:

- `detection_to_build_ms`
- `build_ms`
- `sign_ms`
- `submit_rtt_ms`
- `submit_to_processed_ms`
- `submit_to_landed_ms`
- `detection_to_landed_ms`
- `detection_slot`, `submission_slot`, `landed_slot`
- `slots_to_land`, plus detection- and launch-relative slot deltas

Provider acknowledgement is not landing. Processed/confirmed/finalized fields
represent Hunter's first observation at or after that commitment, subject to
polling interval and RPC visibility.

## Detection sources

All four existing sources record event arrival, processing start, and trade
request creation. Logs and blockSubscribe record the notification context slot;
Yellowstone records the transaction-update slot; PumpPortal records WebSocket
arrival but leaves slot fields unknown. PumpPortal's authoritative curve/mint
refresh records start, end, and batched account-read duration. The refresh
remains fail-closed and is not removed for speed.

Yellowstone continues to use a processed transaction subscription filtered by
the platform program, rejects failed transactions, and prefers authoritative
CreateEvent logs. Milestone 3 adds measurement but does not weaken that state
contract or replace the detector.

Configurable latency budgets emit warnings for slow detection processing,
quote generation, blockhash retrieval, build, signing, or submission RTT. They
are observational unless a future explicit risk rule says otherwise.
