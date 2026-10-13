"""Deployment preflight and isolated, explicitly passive service entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import yaml

from config_loader import load_bot_config
from execution.providers.factory import routing_config_from_dict
from operations.config import DeploymentConfigError, require, validate_deployment
from operations.storage import storage_probe


def preflight(config: dict[str, Any]) -> dict[str, object]:
    """Check local prerequisites without connecting or opening trading data."""
    errors: list[str] = []
    warnings = [
        "Provider/feed connectivity has NOT been tested.",
        "Local checks do not verify an Amsterdam host or future disk writes.",
    ]
    report: dict[str, object] = {
        "errors": errors,
        "warnings": warnings,
        "mode": "unknown",
        "python": platform.python_version(),
        "host_platform": platform.system(),
        "connectivity_tested": False,
        "live_trading": False,
        "log_destination": "stderr / systemd journal",
        "log_write_tested": False,
        "configured_database_integrity": "not_checked (preflight uses disposable data)",
    }
    try:
        validate_deployment(config)
    except (ValueError, TypeError, AttributeError) as error:
        errors.append(
            str(error)
            if isinstance(error, DeploymentConfigError)
            else "Invalid production configuration structure"
        )
        return report
    report.update(
        mode="shadow_non_trading", telegram="disabled", terminal_ui="disabled"
    )
    if sys.version_info < (3, 11):  # noqa: UP036 - diagnostic for deployed interpreters
        errors.append("Python 3.11 or newer is required")
    if platform.system() != "Linux":
        warnings.append(
            "systemd deployment requires Linux; this is a local diagnostic only."
        )
    dependencies = {}
    for name in ("websockets", "solders", "solana", "PyYAML", "grpcio", "aiohttp"):
        try:
            dependencies[name] = version(name)
        except PackageNotFoundError:
            errors.append(f"Missing dependency: {name}")
    report["dependencies"] = dependencies
    path = Path(config["storage"]["shadow_database_path"])
    report["data_directory"] = str(path.parent)
    report["database"] = str(path)
    report["production_database_opened"] = False
    try:
        report["storage"] = storage_probe(
            path.parent, config["deployment"].get("minimum_free_bytes", 100_000_000)
        )
    except (OSError, ValueError) as error:
        errors.append(
            str(error)
            if isinstance(error, DeploymentConfigError)
            else f"Storage probe failed: {type(error).__name__}"
        )
    except Exception as error:  # noqa: BLE001 - reduce driver errors to a safe class
        errors.append(f"SQLite probe failed: {type(error).__name__}")
    report["feed"] = {
        "source": config["filters"]["listener_type"],
        "state": "configured",
        "receiving": "unknown",
    }
    routing = routing_config_from_dict(config.get("execution"))
    report["route_mode"] = routing.mode.value
    report["providers"] = [
        {
            "id": item.endpoint_id,
            "kind": item.kind.value,
            "roles": sorted(role.value for role in item.roles),
            "state": "configured" if item.enabled else "disabled",
            "active": False,
        }
        for item in routing.providers
    ]
    report["cache_state"] = {
        "blockhash": "disabled",
        "priority_fee": "disabled",
        "jito_tip": "disabled",
    }
    return report


def main() -> None:
    """Run preflight by default; shadow ingestion requires a separate command."""
    parser = argparse.ArgumentParser(
        description="Hunter deployment preparation (no economic actions)"
    )
    parser.add_argument("command", choices=("preflight", "shadow"))
    parser.add_argument("config")
    args = parser.parse_args()
    try:
        config = load_bot_config(args.config)
        require("deployment" in config, "deployment")
        report = preflight(config)
    except (OSError, ValueError, TypeError, yaml.YAMLError) as error:
        # Loader's environment error contains only the missing variable name.
        message = (
            str(error)
            if isinstance(error, DeploymentConfigError)
            or str(error).startswith("Environment variable '")
            else f"Configuration load failed: {type(error).__name__}"
        )
        print(json.dumps({"errors": [message], "connectivity_tested": False}))
        raise SystemExit(2) from None
    print(json.dumps(report, indent=2, sort_keys=True), flush=True)
    if report["errors"]:
        raise SystemExit(2)
    if args.command == "shadow":
        from operations.shadow import run_shadow  # noqa: PLC0415

        try:
            asyncio.run(run_shadow(config))
        except Exception as error:  # noqa: BLE001 - keep raw provider errors off stderr
            print(
                json.dumps(
                    {"shadow_failed": type(error).__name__, "live_trading": False}
                )
            )
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
