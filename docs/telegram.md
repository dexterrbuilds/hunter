# Private Telegram control

Hunter can attach one optional, private-owner Telegram control service to each
bot runtime. It is a thin adapter over `HunterApplication`: it does not build
transactions, select providers, store positions, or bypass runtime authorization,
risk, ownership, idempotency, launch/fleet orchestration, or execution accounting.
Automatic trading never waits for the Telegram Bot API.

## Security model

Telegram control is disabled by default. When enabled, Hunter authenticates the
numeric Telegram user ID before parsing or dispatching a command. Usernames and
display names have no authority. Private chats are required by default; group,
supergroup, and channel commands receive only a neutral rejection.

The bot token is resolved from the environment variable named by
`token_env_var`. It must not be placed in YAML. Hunter registers the resolved
value with centralized redaction, excludes it from runtime status, and does not
put secrets or executable parameters in callback data.

```yaml
telegram:
  enabled: false
  owner_user_ids: []
  private_chat_only: true
  token_env_var: TELEGRAM_BOT_TOKEN
  confirmation_ttl_seconds: 45
  kill_confirmation_ttl_seconds: 20
  maximum_pending_actions: 64
  notification_queue_size: 256
  command_limit: 30
  command_interval_seconds: 60
  notification_categories: [all]
```

Set the token only in the local secret environment:

```dotenv
TELEGRAM_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
```

Enabling Telegram with no positive numeric owner ID or no token environment
value fails closed before the service is composed. Disabled Telegram resolves
no token, creates no client, starts no polling, and has no runtime cost.

## Commands

| Area | Commands |
| --- | --- |
| Runtime | `/start`, `/help`, `/status`, `/enable`, `/disable`, `/kill` |
| Trading | `/balance`, `/buy <mint> <amount> [quote]`, `/sell <position-id> <percentage%>` |
| Positions | `/positions`, `/position <id-or-mint>`, `/pnl [id-or-mint]` |
| Execution | `/executions [limit]`, `/tx <execution-id-or-signature>` |
| Infrastructure | `/providers`, `/feeds`, `/infra`, `/settings`, `/risk` |
| Tracking | `/tracked`, `/wallets` |
| Fleet | `/fleet [plan-id]`, `/fleet exit <plan-id> <trigger> <mode> <slippage-bps>` |
| Launch | `/launch ...` when a typed deployment launch resolver is configured |

Tracked-wallet configuration is read-only in this milestone. `/launch` never
acts as a free-form transaction builder: it is available only when the existing
TokenLaunchService and a deployment-supplied resolver can produce a typed
`TokenLaunchRequest`. Without both capabilities it returns a safe unavailable
message.

## Economic confirmation

Buy, sell, fleet exit, launch, enable, disable, and kill actions use server-side
pending state. Telegram callback data contains only `confirm:<nonce>` or
`cancel:<nonce>`. A pending action is bound to its numeric owner ID and private
chat ID, expires on a monotonic deadline, and is atomically claimed before any
await. Confirmed, cancelled, expired, failed, malformed, or replayed callbacks
cannot execute again. Pending confirmations are deliberately ephemeral and all
become invalid after restart.

Preview is not authorization. Confirmation rechecks owner, chat, TTL, runtime
state, kill switch, trading permission, managed-position state, risk, and plan
identity through the application boundary immediately before execution. A
state change between preview and confirmation fails closed.

Manual sells use the existing raw integer amount path and require an owned,
managed position. Under Hunter's runtime safety model, a valid defensive sell
may remain available while trading is disabled or the kill switch is active.
Telegram cannot widen that permission. `/kill` blocks new exposure immediately;
it does not liquidate, cancel submitted signatures, stop monitoring, or block
managed defensive exits.

## Notifications and performance

Application events enter a bounded, prioritized Telegram queue with four
classes: critical, high, normal, and debug. Publishers use a non-blocking queue
insertion; a separate worker performs Bot API I/O. Under pressure, a higher
priority event may replace a lower priority event, and drops/errors are counted.
Duplicate event identities are suppressed within a bounded cache.

Supported event categories include runtime/control state, positions and managed
exits, execution status, provider/feed degradation, recovery issues, tracked
CREATE/BUY, launch/bundle status, and fleet exits. A Telegram outage affects the
optional interface status, not detection, quoting, building, signing, routing,
sending, recovery, or exits.

Metrics are visible through `/infra`: received/rejected commands, unauthorized
attempts, callbacks/replays, pending/expired actions, queued/sent/dropped
notifications, and Telegram consumer errors. Operational command logs contain
only command type, numeric user/chat IDs, result category, and latency.

## Lifecycle and troubleshooting

Hunter composes Telegram after recovery and infrastructure warm-up. Its bounded
startup runs as an optional supervised task, so a slow or unavailable Bot API
cannot delay detection readiness. Shutdown first stops commands and cancels
pending confirmations, then boundedly drains notifications and closes polling.

- `failed` at startup: check that the token environment value is present and
  valid, then inspect redacted operational logs.
- Neutral rejection: use an allowlisted numeric owner ID in a private chat.
- Expired/stale confirmation: issue the original command again and review the
  new preview.
- Entry rejected: inspect `/status` and `/risk`; trading disable, kill switch,
  readiness, balance, fee, or exposure controls may have changed.
- Telegram unavailable: Hunter continues running. Restore connectivity; do not
  weaken risk or private-chat rules to fix transport problems.

Telegram itself remains an external control channel. A compromised authorized
Telegram account can request actions within configured Hunter limits. Use a
dedicated bot, restrict the allowlist, secure the Telegram account with strong
two-factor authentication, keep risk limits enforced, and use a dedicated
trading wallet with limited capital.
