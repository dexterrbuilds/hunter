# Amsterdam deployment preparation

Hunter M6 provides a Linux systemd package for **passive, non-trading observation**.
No Amsterdam host has been provisioned or tested. Amsterdam is an operator-supplied
deployment label, not a measured location or latency advantage. These instructions
are examples for an authorized administrator; no remote commands were executed.

## Existing startup and the M6 boundary

Development `hunter` scans bot YAML files and calls `start_bot`, which constructs
`UniversalTrader` and `HunterApplication`. That runtime recovers durable positions,
warms infrastructure, activates monitors, then starts detection. Trading-disable
and the kill switch intentionally allow managed defensive exits. They do not
guarantee that a process cannot spend funds, and are not shadow mode.

The `deployment` section is an extension of the existing YAML loader. It selects
the M6 production shadow profile before trader construction. The dedicated
`python -m operations.cli shadow CONFIG` entrypoint avoids bot-directory scanning.
Use this entrypoint for deployment, with one service per observation database.

The shadow runtime reuses existing token listeners and detection metadata. It
does not construct a wallet, signer, trader, position service, submission router,
launch service, or fleet. It does not open a trading database. The application
authorization boundary also rejects every economic action and attempts to enable
trading when a deployment profile is present, as defense in depth.

Supported shadow listeners: `logs`, `blocks`, and `pumpportal`, with existing
platform compatibility (`logs` is Pump.fun-only). Geyser, RabbitStream, Riptide,
aggregate feeds, tracked-wallet observation, Telegram and TUI are not composed
by this restricted service. Their existing development/runtime implementations
are unchanged. There is no shadow strategy engine, simulated fill, simulated
position, PnL, authoritative PumpPortal state refresh, or automatic routing change.
PumpPortal events are observations, not evidence that a trade can be built safely.

## Configuration modes

| Configuration | Behavior |
| --- | --- |
| No `deployment` section | Existing development bot semantics; may trade if configured. Do not use for this deployment. |
| `deployment.environment: production`, mode omitted or `shadow` | Strict, passive profile; `live_trading_opt_in` must be absent or literally `false`. |
| `mode: live`, unknown mode, or live opt-in | Rejected by the M6 profile. Live deployment is not implemented in this package. |

Use [the example](../config/examples/hunter-amsterdam.yaml). Unknown production
keys and unsupported combinations fail validation. The restricted profile rejects
trade settings, wallet secrets, cleanup actions, and economic benchmark settings.
Required URLs must be TLS URLs with valid ports; unresolved environment variables
and recognized placeholder strings are rejected. Syntax validation cannot prove
that a real credential is valid. Optional disabled interfaces require no secrets
or framework initialization. No `.env` is loaded by a deployment profile.

Provider definitions can be included in `execution` for the existing adapter and
capability validation, but **no provider transport is initialized**. Existing
routing rules are not changed. Preflight only reports configured/disabled state.
Blockhash, priority-fee and tip caches are disabled in shadow; their freshness is
unmeasured. Validate provider connectivity separately in a later authorized phase.

## Host prerequisites and release installation

Target: Linux with systemd and local persistent storage, preferably x86-64 Ubuntu
24.04 with Python 3.12. Hunter declares Python 3.11+; ARM64 depends on availability
of the locked native wheels. Linux service behavior and wheel availability must
be verified on the actual host. Local validation was on macOS, not Amsterdam.

Install a reviewed uv release (local workflow uses uv 0.12.7), Git, Python and the
host's CA certificates using an administrator-approved procedure. Do not disable
TLS validation. Do not run an unattended distribution upgrade to install Hunter.

Example administrator commands after host access and release selection:

```sh
sudo useradd --system --user-group --home-dir /var/lib/hunter --shell /usr/sbin/nologin hunter
sudo install -d -m 0755 /opt/hunter/releases
sudo install -d -m 0750 -o root -g hunter /etc/hunter
sudo install -d -m 0700 -o hunter -g hunter /var/lib/hunter/shadow
```

Stage the reviewed Git commit into `/opt/hunter/releases/RELEASE` as your deployment
administrator (replace `RELEASE` explicitly). Do not build as root or as the runtime
service account. Inside that release, use:

```sh
uv sync --locked --no-dev --python 3.12
uv pip check
uv lock --check --offline
```

This installs the project's locked dependencies, including optional interface
packages; disabled interfaces never instantiate their frameworks. Keep the release
and its virtual environment readable/executable by `hunter` and non-writable by
that account. Select it with the administrator-managed `/opt/hunter/current`
symlink. The data directory itself must not be a symlink. A release should never
contain a wallet key, environment file or copied live database.

## Secrets and service setup

Copy the example to `/etc/hunter/shadow.yaml`, owned by root with group `hunter`,
mode 0640. Create `/etc/hunter/hunter.env` separately, owned by root, mode 0600.
It is read by systemd before privileges are dropped. Supply only the selected
feed's credential-bearing URL, for example the `SOLANA_NODE_WSS_ENDPOINT` setting.
Do not copy a production trading wallet into this file. Do not place real values
in Git, shell history, command arguments, tickets or diagnostics.

The unit uses a dedicated non-root user, a read-only release, `UMask=0077`, private
temporary/device access, an empty capability set and restricted writable paths.
No HTTP server or inbound port is added. Existing HTTPS/WSS egress is required
only when shadow ingestion is deliberately started. The sample imposes no CPU or
memory caps that could silently starve a listener; size them from host observation.

```sh
sudo install -m 0644 deploy/systemd/hunter-shadow.service /etc/systemd/system/hunter-shadow.service
sudo systemd-analyze verify /etc/systemd/system/hunter-shadow.service
sudo systemctl daemon-reload
```

Review paths before running these examples. Do not use `hunter` to launch the
shadow unit. The service is not enabled on boot automatically by these commands.

## Preflight before network ingestion

Run as `hunter`, with the service's environment supplied securely. One way is an
administrator-created transient unit, with the same identity and EnvironmentFile:

```sh
sudo systemd-run --wait --pipe --collect --uid=hunter --gid=hunter \
  --property=EnvironmentFile=/etc/hunter/hunter.env \
  --property=WorkingDirectory=/opt/hunter/current \
  /opt/hunter/current/.venv/bin/python -m operations.cli preflight /etc/hunter/shadow.yaml
```

Preflight returns JSON and exit code 2 for blocking failures. It checks mode,
required variable names, syntax/capabilities, dependency presence, directory owner
and permissions, available disk space, and a disposable SQLite write/reopen in
the configured directory. It removes the diagnostic directory afterward. It
does not open or validate the configured observation database, mutate trading
records, load keys, or contact providers. Configuration errors use setting names;
driver errors report their class. The report distinguishes warnings from errors.
It identifies journald/stderr as the destination but does not claim to have tested
the host journal. Free-space checks do not guarantee future writes will succeed.

Once preflight succeeds and the feed subscription is authorized, start passive
ingestion explicitly:

```sh
sudo systemctl start hunter-shadow
sudo systemctl status hunter-shadow
sudo journalctl -u hunter-shadow -f
```

Only after reviewing host behavior, decide separately whether to enable boot start:
`sudo systemctl enable hunter-shadow`. No example enables live trading.

## Readiness and operations

The service is `Type=simple`: systemd's active state means the process is running,
not that the feed is connected or receiving. Structured `shadow_status` records
report non-trading mode, session recovery duration, observation count and age,
queue depth/drops, and persistence/task failures, every 30 seconds by default.

| State | Meaning |
| --- | --- |
| configured (preflight) | Configuration parsed; connectivity untested. |
| initializing | Storage initialization/recovery is in progress. |
| unknown | Listener task started but no event has been observed. |
| receiving | A parsed event reached the callback. |
| stale | No parsed event within the configured observation-age threshold. |
| failed | A storage/task error escaped; process exits nonzero. |
| stopped | Producers stopped and queued observations were flushed. |

Staleness may mean quiet markets or broken delivery. It is not a transport proof.
Legacy listeners do not expose typed connection/reconnect hooks; connection state
and reconnect counters remain unknown. Reconnect logs retain their existing fixed
delays and redaction. There is no aggressive new retry loop or fake health result.
The service does not verify selected provider routes, cache warm-up or latency.

## Persistence, interruption and shutdown

`/var/lib/hunter/shadow/observations.sqlite3` stores operational sessions and public
mint/source/detection timestamps/slot observations, never private keys or PnL.
Directories require owner-only access; database/lock files use 0600. WAL with
`synchronous=FULL` and a two-second SQLite busy timeout is used. Schema version and
application identity are verified before updates; foreign/trading/corrupt/future
schema databases are refused, never recreated. One advisory file lock prevents
two observers from marking each other's sessions interrupted.

Storage is initialized and interrupted observation sessions are marked before
detection starts. Recovery never replays economic actions. A bounded queue drops
new observations under pressure and counts drops; a single off-loop writer batches
up to 128 observations. Mint observations are deduplicated within a session.
Committed observations survive restart; events still in memory can be lost on a
crash. Repeated mints in a later session are legitimate new observations.

SIGINT and SIGTERM request graceful stop: cancel the listener, drain the queue
within the configured timeout, finish the session, close SQLite, then exit.
Unexpected detector/persistence failures exit nonzero. Failed flushes retain a
running session marker, recovered as interrupted next start. SQLite failures are
not swallowed as success. A stuck native filesystem call cannot be cancelled by
Python; systemd's 30-second stop timeout is the hard process boundary. Committed
data remains SQLite's responsibility; do not assume a killed process flushed RAM.

Systemd restarts failed processes after ten seconds, with five starts per five
minutes. Repeated initialization failures require investigation, not deleting
the database. Normal `systemctl stop` does not restart. Example restart:
`sudo systemctl restart hunter-shadow`.

The existing live position store remains unchanged: WAL/NORMAL, serialized local
transactions, durable execution identity and ambiguous-outcome reconciliation.
Its runtime recovery tests remain part of the full offline suite. M6 does not run
that recovery against real funds or claim to validate a production trading DB.

## Backup, rollback and disk monitoring

For a simple consistent observation backup, stop the service cleanly, then copy
the database and any remaining WAL/SHM files together into a private backup
directory. Alternatively use SQLite's online backup API with appropriate ownership.
Do not copy only the main file while a writer is active. Test restoration into a
separate private directory and verify integrity before replacing any data.

For rollback: stop the service, back up state, point `/opt/hunter/current` to the
previous reviewed release, check schema compatibility and rerun preflight, then
start. Never delete data to force a rollback. M6 shadow schema is version 1;
unknown future versions fail closed. Do not point a pre-M6 release at the shadow
service entrypoint it does not provide.

Monitor disk space, inode availability and journal retention externally. SQLite
observations have no automatic retention policy in M6; plan archival before long
runs. Local disks are recommended; network filesystems need separate SQLite/WAL
and locking validation. Configure journald retention through the host's approved
operations procedure. Logs and private host configuration stay outside Git.

## Troubleshooting and remaining host work

- Missing variable: supply the named setting through the service environment.
- Storage permission failure: verify owner, mode 0700 directory, 0600 file, parent
  traversal, and writable space for WAL/SHM/lock files. Do not make directories 0777.
- Locked DB: find the other process; do not remove its active lock file.
- Unknown feed: inspect sanitized connection logs and verify subscription support
  with the operator. Preflight has deliberately not checked network reachability.
- Optional interface enabled: this restricted profile refuses it. Continue using
  the normal runtime's separately validated optional services outside M6 shadow.
- Configuration error: unsupported fields are rejected; do not rename `deployment`
  to bypass the production guard or substitute a trading bot YAML.

Manual work still required: provision an approved Amsterdam host; verify Linux,
Python, wheels and systemd directives; install a reviewed release; create the
service identity and paths; securely provide approved feed credentials; run host
preflight and service-template verification; explicitly authorize shadow feed
connections; observe restart/signal behavior; configure retention/backups and
restore drills. No live deployment, performance ranking or production-readiness
claim follows from the offline milestone tests.
