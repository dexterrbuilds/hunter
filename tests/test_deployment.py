"""Offline deployment checks: no listener connects and no signer is loaded."""

from __future__ import annotations

import ast
import asyncio
import contextlib
import io
import json
import os
import signal
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

import test_runtime_composition as fixtures
import yaml

from application.runtime import HunterApplication
from config_loader import load_bot_config
from domain.intents import TradeAction, TradeIntentSource
from execution.errors import ExecutionError
from operations.cli import main, preflight
from operations.config import DeploymentConfigError, endpoint, validate_deployment
from operations.shadow import ShadowRuntime, run_shadow
from operations.storage import ShadowStore, prepare_directory, storage_probe

ROOT = Path(__file__).resolve().parents[1]


def config(path):
    return {
        "name": "shadow-test",
        "deployment": {
            "environment": "production",
            "minimum_free_bytes": 0,
            "report_interval_seconds": 3600,
        },
        "storage": {"shadow_database_path": str(path)},
        "risk": {"enforce": True, "trading_enabled": False},
        "filters": {"listener_type": "logs"},
        "wss_endpoint": "wss://localhost:8900",
    }


class FakeListener:
    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = False

    async def listen_for_tokens(self, callback):
        self.started.set()
        try:
            await callback(
                fixtures.TokenInfo(
                    "fake", "F", "", fixtures.MINT, fixtures.Platform.PUMP_FUN
                )
            )
            await asyncio.Event().wait()
        finally:
            self.cancelled = True


class DeploymentConfigTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config(Path("/var/lib/hunter/shadow/test.sqlite3"))

    def test_default_is_shadow_without_live_opt_in(self):
        validate_deployment(self.cfg)

    def test_mode_typos_and_live_rejected(self):
        for mode in ("live", "Shadow", "shdaow", True, None):
            with self.subTest(mode=mode), self.assertRaises(DeploymentConfigError):
                self.cfg["deployment"]["mode"] = mode
                validate_deployment(self.cfg)

    def test_opt_in_cannot_enable_this_profile(self):
        for value in (True, "false", 0, 1, None):
            self.cfg["deployment"]["live_trading_opt_in"] = value
            with self.assertRaises(DeploymentConfigError):
                validate_deployment(self.cfg)

    def test_risk_and_no_wallet_required(self):
        for field, value in (
            ("risk", {}),
            ("private_key", "do-not-print"),
            ("env_file", ".env"),
        ):
            candidate = deepcopy(self.cfg)
            candidate[field] = value
            with self.assertRaises(DeploymentConfigError) as caught:
                validate_deployment(candidate)
            self.assertNotIn("do-not-print", str(caught.exception))

    def test_optional_services_must_be_disabled(self):
        for name in (
            "telegram",
            "terminal_ui",
            "wallet_tracking",
            "wallet_fleet",
            "token_launch",
        ):
            candidate = deepcopy(self.cfg)
            candidate[name] = {"enabled": False}
            validate_deployment(candidate)
            candidate[name]["enabled"] = True
            with self.assertRaises(DeploymentConfigError):
                validate_deployment(candidate)

    def test_invalid_endpoints_redacted(self):
        for url in (
            "http://localhost",
            "wss://localhost:99999",
            "wss://localhost:abc",
            "wss://",
            "wss://example.com/?api_key=PRIVATE",
            "wss://localhost/${TOKEN}",
            "wss://local host",
        ):
            with (
                self.subTest(url=url),
                self.assertRaises(DeploymentConfigError) as caught,
            ):
                endpoint(url, "wss_endpoint", {"wss"})
            self.assertNotIn(url, str(caught.exception))

    def test_numeric_bounds(self):
        for key in ("queue_size", "stale_after_seconds", "shutdown_timeout_seconds"):
            for value in (-1, 0, True, "10", float("nan"), float("inf"), 10**20):
                candidate = deepcopy(self.cfg)
                candidate["deployment"][key] = value
                with (
                    self.subTest(key=key, value=value),
                    self.assertRaises(DeploymentConfigError),
                ):
                    validate_deployment(candidate)

    def test_unknown_keys_fail_closed(self):
        self.cfg["deployment"]["live_trading_optin"] = True
        with self.assertRaises(DeploymentConfigError):
            validate_deployment(self.cfg)

    def test_persistent_and_separate_database(self):
        for path in (
            "data/foo.db",
            "/tmp/foo.db",  # noqa: S108 - deliberately rejected path
            "/var/lib/../tmp/foo.db",
            ":memory:",
        ):
            self.cfg["storage"]["shadow_database_path"] = path
            with self.assertRaises(DeploymentConfigError):
                validate_deployment(self.cfg)

    def test_incompatible_listener_and_unsupported_profile(self):
        self.cfg["platform"] = "lets_bonk"
        with self.assertRaises(DeploymentConfigError):
            validate_deployment(self.cfg)
        self.cfg["filters"]["listener_type"] = "aggregate"
        with self.assertRaises(DeploymentConfigError):
            validate_deployment(self.cfg)

    def test_provider_capability_validation_safe_errors(self):
        self.cfg["execution"] = {"mode": "invalid-secret-value"}
        with self.assertRaises(DeploymentConfigError) as caught:
            validate_deployment(self.cfg)
        self.assertNotIn("invalid-secret-value", str(caught.exception))

    def test_example_load_without_wallet_or_optional_secrets(self):
        with patch.dict(
            os.environ, {"SOLANA_NODE_WSS_ENDPOINT": "wss://localhost:8900"}, clear=True
        ):
            loaded = load_bot_config(
                str(ROOT / "config/examples/hunter-amsterdam.yaml")
            )
        self.assertNotIn("private_key", loaded)
        self.assertFalse(loaded["risk"]["trading_enabled"])

    def test_disabled_interfaces_do_not_resolve_unused_credentials(self):
        candidate = deepcopy(self.cfg)
        candidate["telegram"] = {"enabled": False, "token": "${UNUSED_TOKEN}"}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.yaml"
            path.write_text(yaml.safe_dump(candidate))
            with patch.dict(os.environ, {}, clear=True):
                load_bot_config(str(path))


class DeploymentStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve() / "shadow.db"
        # Disposable tests may live under /tmp on Linux; production path rules
        # are tested separately without this scoped override.
        self.path_guard = patch("operations.config.validate_persistent_path")
        self.path_guard.start()
        self.addCleanup(self.path_guard.stop)

    def test_private_permissions_wal_and_committed_restart(self):
        store = ShadowStore(self.path)
        self.assertEqual(
            store.connection.execute("PRAGMA journal_mode").fetchone()[0], "wal"
        )
        store.start("one", "now")
        store.record([("one", "mint", "logs", "now", 1, None)] * 2)
        store.close()
        store = ShadowStore(self.path)
        self.addCleanup(store.close)
        self.assertEqual(store.start("two", "later"), 1)
        self.assertEqual(
            store.connection.execute("SELECT count(*) FROM observations").fetchone()[0],
            1,
        )
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_never_reuses_trading_database(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute("CREATE TABLE positions(id TEXT)")
        self.path.chmod(0o600)
        before = self.path.read_bytes()
        with self.assertRaises(DeploymentConfigError):
            ShadowStore(self.path)
        self.assertEqual(before, self.path.read_bytes())

    def test_second_observer_cannot_recover_running_owner(self):
        store = ShadowStore(self.path)
        self.addCleanup(store.close)
        store.start("owner", "now")
        with self.assertRaises(BlockingIOError):
            ShadowStore(self.path)
        self.assertEqual(
            store.connection.execute("SELECT status FROM sessions").fetchone()[0],
            "running",
        )

    def test_future_schema_rejected(self):
        store = ShadowStore(self.path)
        store.connection.execute("PRAGMA user_version=999")
        store.close()
        with self.assertRaises(DeploymentConfigError):
            ShadowStore(self.path)

    def test_corruption_preserved(self):
        self.path.write_bytes(b"not a database")
        self.path.chmod(0o600)
        with self.assertRaises(sqlite3.DatabaseError):
            ShadowStore(self.path)
        self.assertEqual(self.path.read_bytes(), b"not a database")

    def test_permissions_and_symlinks_rejected(self):
        directory = self.path.parent / "unsafe"
        directory.mkdir(mode=0o755)
        with self.assertRaises(DeploymentConfigError):
            prepare_directory(directory)
        link = self.path.parent / "link"
        link.symlink_to(self.path.parent, target_is_directory=True)
        with self.assertRaises(DeploymentConfigError):
            prepare_directory(link)

    def test_preflight_does_not_touch_existing_database(self):
        self.path.write_bytes(b"production sentinel")
        before = self.path.stat()
        report = preflight(config(self.path))
        self.assertEqual(report["errors"], [])
        self.assertEqual(before.st_mtime_ns, self.path.stat().st_mtime_ns)
        self.assertEqual(self.path.read_bytes(), b"production sentinel")
        self.assertFalse(report["connectivity_tested"])
        self.assertEqual(report["feed"]["state"], "configured")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_preflight_disk_permission_failures(self):
        with (
            patch(
                "operations.storage.shutil.disk_usage",
                return_value=type("Usage", (), {"free": 0})(),
            ),
            self.assertRaises(DeploymentConfigError),
        ):
            storage_probe(self.path.parent, 1)
        with patch("operations.storage.ShadowStore", side_effect=PermissionError):
            self.assertTrue(preflight(config(self.path))["errors"])

    def test_database_busy_is_bounded_and_visible(self):
        store = ShadowStore(self.path)
        self.addCleanup(store.close)
        store.connection.execute("PRAGMA busy_timeout=1")
        lock = sqlite3.connect(self.path)
        self.addCleanup(lock.close)
        lock.execute("BEGIN IMMEDIATE")
        with self.assertRaises(sqlite3.OperationalError):
            store.start("one", "now")
        lock.rollback()

    def test_cli_missing_environment_nonzero_and_no_secret(self):
        output = io.StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "sys.argv",
                [
                    "hunter-ops",
                    "preflight",
                    str(ROOT / "config/examples/hunter-amsterdam.yaml"),
                ],
            ),
            contextlib.redirect_stdout(output),
            self.assertRaises(SystemExit) as caught,
        ):
            main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("SOLANA_NODE_WSS_ENDPOINT", output.getvalue())

    def test_cli_success_is_offline(self):
        output = io.StringIO()
        with (
            patch("operations.cli.load_bot_config", return_value=config(self.path)),
            patch("sys.argv", ["hunter-ops", "preflight", "fake"]),
            contextlib.redirect_stdout(output),
        ):
            main()
        self.assertFalse(json.loads(output.getvalue())["connectivity_tested"])

    def test_malformed_yaml_does_not_expose_values(self):
        path = self.path.parent / "bad.yaml"
        path.write_text("deployment: [SECRET_VALUE: {broken")
        output = io.StringIO()
        with (
            patch("sys.argv", ["hunter-ops", "preflight", str(path)]),
            contextlib.redirect_stdout(output),
            self.assertRaises(SystemExit),
        ):
            main()
        self.assertNotIn("SECRET_VALUE", output.getvalue())


class ShadowRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve() / "shadow.db"
        self.path_guard = patch("operations.config.validate_persistent_path")
        self.path_guard.start()
        self.addCleanup(self.path_guard.stop)
        self.cfg = config(self.path)
        self.listener = FakeListener()

    async def test_recovery_before_detection_flush_and_stop(self):
        runtime = ShadowRuntime(self.cfg, self.listener)
        task = asyncio.create_task(runtime.run())
        await asyncio.wait_for(self.listener.started.wait(), 3)
        self.assertIsNotNone(runtime.recovery_ms)
        runtime.stop.set()
        await task
        self.assertTrue(self.listener.cancelled)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM observations").fetchone()[0], 1
            )
            self.assertEqual(
                connection.execute("SELECT status FROM sessions").fetchone()[0],
                "stopped",
            )
            self.assertFalse(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='positions'"
                ).fetchall()
            )

    async def test_bounded_queue_and_staleness(self):
        self.cfg["deployment"]["queue_size"] = 1
        runtime = ShadowRuntime(self.cfg, self.listener)
        self.assertEqual(runtime.status()["connection_state"], "unknown")
        token = fixtures.TokenInfo(
            "fake", "F", "", fixtures.MINT, fixtures.Platform.PUMP_FUN
        )
        await runtime.observe(token)
        await runtime.observe(token)
        self.assertEqual(runtime.dropped, 1)
        self.assertEqual(runtime.status()["feed_state"], "receiving")
        runtime.last_observed -= 121
        self.assertEqual(runtime.status()["feed_state"], "stale")

    async def test_persistence_failure_stops_listener(self):
        runtime = ShadowRuntime(self.cfg, self.listener)
        with (
            patch.object(
                ShadowStore, "record", side_effect=sqlite3.OperationalError("disk full")
            ),
            self.assertRaises(sqlite3.OperationalError),
        ):
            await asyncio.wait_for(runtime.run(), 3)
        self.assertTrue(self.listener.cancelled)
        self.assertEqual(runtime.status()["failure"], "OperationalError")

    async def test_failed_recovery_never_starts_detection(self):
        runtime = ShadowRuntime(self.cfg, self.listener)
        with (
            patch("operations.shadow.ShadowStore", side_effect=PermissionError),
            self.assertRaises(PermissionError),
        ):
            await runtime.run()
        self.assertFalse(self.listener.started.is_set())

    async def test_shutdown_flush_timeout_is_failure_not_clean_stop(self):
        runtime = ShadowRuntime(self.cfg, self.listener)
        runtime.config["deployment"]["shutdown_timeout_seconds"] = 0.01

        async def stalled_writer():
            await asyncio.Event().wait()

        with patch.object(runtime, "_persist", stalled_writer):
            task = asyncio.create_task(runtime.run())
            await asyncio.wait_for(self.listener.started.wait(), 3)
            runtime.stop.set()
            with self.assertRaises(TimeoutError):
                await task
        self.assertEqual(runtime.status()["feed_state"], "failed")
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("SELECT status FROM sessions").fetchone()[0],
                "running",
            )

    async def test_sigterm_sigint_handlers_and_cleanup(self):
        loop = asyncio.get_running_loop()
        callbacks = {}

        def register(sig, callback):
            callbacks[sig] = callback

        async def fake_run(runtime):
            self.assertEqual(set(callbacks), {signal.SIGINT, signal.SIGTERM})
            callbacks[signal.SIGTERM]()
            self.assertTrue(runtime.stop.is_set())

        with (
            patch(
                "monitoring.listener_factory.ListenerFactory.create_listener",
                return_value=self.listener,
            ),
            patch.object(loop, "add_signal_handler", side_effect=register),
            patch.object(loop, "remove_signal_handler") as remove,
            patch.object(ShadowRuntime, "run", fake_run),
        ):
            await run_shadow(self.cfg)
        self.assertEqual(remove.call_count, 2)

    async def test_final_application_boundary_refuses_entries_and_exits(self):
        trader = fixtures.FakeTrader()
        app = HunterApplication(
            {
                "name": "safe",
                "filters": {"listener_type": "logs"},
                "deployment": {"mode": "shadow"},
            },
            trader,
        )
        await app.start(start_detection=False)
        try:
            for source, action in (
                (TradeIntentSource.MANUAL_BUY, TradeAction.BUY),
                (TradeIntentSource.MANUAL_SELL, TradeAction.SELL),
            ):
                with self.assertRaises(ExecutionError):
                    app.authorize_economic_action(source, action, managed_exit=True)
            with self.assertRaises(ValueError):
                app.enable_trading()
            self.assertFalse(app.runtime_status().entries_allowed)
            self.assertFalse(app.runtime_status().defensive_exits_allowed)
        finally:
            await app.shutdown()

    async def test_normal_startup_dispatches_before_trader_construction(self):
        with (
            patch("bot_runner.load_bot_config", return_value=self.cfg),
            patch("operations.shadow.run_shadow", new_callable=AsyncMock) as shadow,
            patch("bot_runner.UniversalTrader") as trader,
            patch("bot_runner.telegram_control_factories_from_config") as telegram,
            patch("bot_runner.terminal_ui_factories_from_config") as terminal,
        ):
            from bot_runner import start_bot  # noqa: PLC0415

            await start_bot("fake")
        shadow.assert_awaited_once()
        trader.assert_not_called()
        telegram.assert_not_called()
        terminal.assert_not_called()


class DeploymentTemplateTests(unittest.TestCase):
    def test_service_paths_and_safety(self):
        service = (ROOT / "deploy/systemd/hunter-shadow.service").read_text()
        for fragment in (
            "User=hunter",
            "UMask=0077",
            "Restart=on-failure",
            "KillSignal=SIGTERM",
            "operations.cli shadow",
            "EnvironmentFile=/etc/hunter/hunter.env",
            "StateDirectory=hunter/shadow",
        ):
            self.assertIn(fragment, service)
        self.assertNotIn("PrivateKey", service)

    def test_shadow_module_has_no_economic_imports(self):
        tree = ast.parse((ROOT / "src/operations/shadow.py").read_text())
        modules = [
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        ]
        for module in modules:
            if module != "execution.detection":
                self.assertFalse(
                    module.startswith(
                        (
                            "core.wallet",
                            "core.client",
                            "trading",
                            "execution",
                            "application",
                        )
                    )
                )

    def test_template_parses(self):
        value = yaml.safe_load(
            (ROOT / "config/examples/hunter-amsterdam.yaml").read_text()
        )
        self.assertFalse(value["deployment"]["live_trading_opt_in"])
