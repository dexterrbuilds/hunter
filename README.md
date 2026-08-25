# Hunter

Hunter is an independently maintained Solana trading-bot project for Pump.fun
and LetsBonk. It is derived from an Apache-2.0-licensed upstream implementation;
see [UPSTREAM.md](UPSTREAM.md) for provenance and licensing details.

> **Safety status:** Hunter is an early baseline, not production-ready software.
> It can sign and submit transactions that control real funds. Read
> [docs/known-risks.md](docs/known-risks.md) before configuring a wallet.

## Currently working

- Pump.fun `buy_v2` and `sell_v2` instruction construction
- SOL and configured non-SOL quote assets
- Token and Token-2022 account handling
- Pump.fun bonding-curve decoding and creation-event parsing
- Token detection through Solana logs, blocks, Yellowstone Geyser, or PumpPortal
- Standard Solana JSON-RPC transaction submission and confirmation
- Fixed or RPC-estimated priority fees and configurable compute limits
- Time-based and take-profit/stop-loss exits
- LetsBonk protocol support inherited from the audited baseline
- Offline protocol and transaction characterization tests

## In development

- Modular trading-engine boundaries
- Persistent positions and restart recovery
- Provider-neutral execution and confirmation services
- Accurate realized PnL and execution telemetry
- Risk, exposure, and fee controls

## Planned

Telegram, Jito, Helius, multi-RPC execution, copy trading, and broader strategy
automation are planned but are not implemented.

## Install

Hunter requires Python 3.11 or newer. Using
[`uv`](https://github.com/astral-sh/uv):

```bash
git clone <your-hunter-repository-url> hunter
cd hunter
uv sync
uv pip install -e .
```

Copy the placeholder environment file and provide credentials locally:

```bash
cp .env.example .env
```

Never commit `.env`, a wallet keypair, an authenticated RPC URL, or a Geyser
token. All example bot configurations are disabled by default.

Run all enabled configurations in `bots/` with:

```bash
hunter
# or
uv run src/bot_runner.py
```

Logs are written under `logs/` and pass through Hunter's credential redactor.
Redaction is a defense-in-depth measure, not permission to log secrets.

## Configuration

The examples in `bots/` document the currently supported settings:

- `trade`: buy amount, quote amounts, slippage, exit behavior, and extreme-fast mode
- `filters`: listener type and token filters
- `priority_fees`: fixed or dynamic compute-unit price
- `compute_units`: optional operation-specific limits
- `retries`: submission attempts and trading delays
- `cleanup`: token-account cleanup policy
- `node.max_rps`: local RPC request-rate limit

Extreme-fast mode trusts complete Pump.fun `CreateEvent` state for its zero-read
path. PumpPortal does not carry the same authoritative state, so Hunter performs
a batched curve-and-mint refresh and skips the token if that state cannot be read
within the configured budget.

## Offline validation

The default test suite does not require credentials and does not submit a
transaction:

```bash
python -m unittest discover -s tests -v
```

The existing standalone verification tools remain available, including:

```bash
python learning-examples/verify_v2_account_layout.py
python learning-examples/verify_extreme_fast_zero_rpc.py
python learning-examples/verify_pumpportal_buy_path.py
python learning-examples/verify_tx_status_checks.py
```

Scripts named `manual_*`, mint-and-buy examples, cleanup tools, and the actual
bot can spend funds. Do not use them as tests.

## Design and risks

- [Execution interfaces](docs/execution-interfaces.md)
- [Execution telemetry](docs/execution-telemetry.md)
- [Known unresolved risks](docs/known-risks.md)
- [Upstream provenance](UPSTREAM.md)

## License

Apache License 2.0. See [LICENSE](LICENSE) and [UPSTREAM.md](UPSTREAM.md).
