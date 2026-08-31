# Pump.fun token launch and wallet fleets

Milestone 3.7 introduces application boundaries for an operator-controlled
Pump.fun launch and its wallet fleet. They are disabled by default and are not
wired to normal bot startup or a public CLI. Metadata upload is outside this
service: the launch request accepts an already hosted `https://`, `ipfs://`, or
`ar://` URI.

## Launch plan

`TokenLaunchRequest` contains public launch metadata, the new mint public key,
non-secret signer references, exact quote amounts, an execution policy, exit
policy, and stable launch ID. Private keys never enter the domain object or
SQLite. The current create builder supports SOL-paired launches only; an SPL
quote request fails closed until the optional quote-account creation form is
independently implemented and characterized.

The Pump.fun `create_v2` and `extend_account` instructions are built from the
vendored IDL account order and discriminators. The existing verified `buy_v2`
factory supplies creator and participant buys. Hunter models create and creator
buy as separate transactions because their combined legacy packet exceeds the
Solana packet-size limit in the current implementation.

```text
plan
  0. create_v2 + extend_account     (creator + mint signatures)
  1. creator buy_v2                 (creator signature)
  2. participant A buy_v2           (wallet A signature)
  3. participant B buy_v2           (wallet B signature)
```

Every component has a distinct logical execution ID and signature. A single
fresh, age-checked blockhash is shared across the frozen plan. Preparation and
signing are bounded and concurrent; serialized bytes are not rebuilt during a
submission attempt.

## Jito bundles and capacity

Hunter's Jito bundle adapter uses `sendBundle` with ordered base64 transaction
bytes and `getBundleStatuses` for recovery. Current official Jito documentation
defines a bundle as at most five transactions, executed sequentially and
atomically in one slot. Hunter enforces that capability instead of silently
truncating a plan. With separate create and creator-buy components, the current
five-transaction limit leaves room for three participant buys.

The bundle tip is separate from CU priority fees, appears exactly once across
the bundle, and is checked against provider and launch risk caps. A bundle ID is
an acknowledgement, not proof of landing. An ambiguous timeout or restart moves
the plan to reconciliation and causes status inspection—not a rebuilt launch.

`parallel_fast` and `sequential` are explicit non-atomic alternatives. They can
partially land; any partial or ambiguous result requires component-signature
reconciliation. The same-signature Sender Max race is not used to imitate a
multi-transaction bundle.

## Mandatory launch risk

Multi-wallet launch execution refuses to run unless dedicated enforcement is
active. Preflight checks every signer/public-key match and wallet balance, then
applies maximum creator buy, participant buy, aggregate spend, wallet count,
priority fees, bundle tip, combined cost/rent, minimum reserve, simultaneous
launch exposure, bundle capacity, quote mint, quote decimals, blockhash age, and
configured provider capability. Rejection occurs before signing where possible.

## Fleet accounting and exits

Authoritative landed buy effects can create fleet positions containing fleet
and launch IDs, mint/quote, wallet role, buy signature, exact inventory, quote
cost basis, known SOL costs, status, marry flag, and persisted scheduled-exit
time. No signer material is stored.

Fleet exit decisions use expected curve proceeds, not spot-price
multiplication. Profit target and TP/SL compare expected portfolio quote value
with aggregate actual cost basis. SOL network costs contribute to net return
only for SOL-quoted fleets. For USDC or another SPL quote, SOL costs stay
separate and net quote return remains unknown without an explicit FX source.

Exit policies are `bundle`, `parallel_fast`, or `sequential`. Each sell is a
universal high/critical-urgency `TradeIntent` with a stable per-position ID.
Claims and updates run off the async hot path. A bundle can fail atomically when
one component is stale, so inventory/balance/account validation belongs before
signing; `exclude_invalid_positions` must be chosen explicitly. `marry_mode`
blocks profit, time, TP, and SL auto-exits but does not silently redefine manual
or emergency requests.

## Restart behavior

Launch plans, ordered components, signatures, blockhash, bundle ID, states,
fleet positions, scheduled times, and pending exit identities are durable.
Recovery inspects a submitted bundle or exposes a pending fleet exit; it never
resubmits solely because Hunter restarted. Missing provider access, temporary
non-observation, partial non-bundle effects, and mismatched balances require
reconciliation.

## Current limitations

- no metadata uploader, treasury distributor, Telegram surface, or live launch
  command is enabled;
- multi-wallet orchestration has only offline/fake-provider validation;
- Jito capacity and endpoint semantics must be rechecked against current
  provider documentation before funded use;
- participant buys depend on authoritative current Pump curve/fee state through
  the existing buy instruction factory;
- PumpSwap migration exits remain on the existing venue-aware path;
- raw Triton shreds still require a reviewed provider SDK/sidecar decoder.
