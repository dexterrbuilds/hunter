"""Run the upstream offline verification tools as repeatable tests."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

OFFLINE_VERIFIERS = (
    "verify_v2_account_layout.py",
    "verify_extreme_fast_zero_rpc.py",
    "verify_pumpportal_buy_path.py",
    "verify_tp_sl_exit_price.py",
    "verify_tx_status_checks.py",
    "verify_create_v2_optional_args.py",
)

SENSITIVE_ENVIRONMENT_KEYS = (
    "SOLANA_NODE_RPC_ENDPOINT",
    "SOLANA_NODE_WSS_ENDPOINT",
    "SOLANA_PRIVATE_KEY",
    "GEYSER_ENDPOINT",
    "GEYSER_API_TOKEN",
    "TELEGRAM_BOT_TOKEN",
)


class OfflineVerificationScriptTests(unittest.TestCase):
    """Require every inherited offline verifier to keep passing."""

    def test_offline_verification_scripts(self) -> None:
        environment = os.environ.copy()
        for key in SENSITIVE_ENVIRONMENT_KEYS:
            environment.pop(key, None)
        environment["PYTHONPATH"] = str(ROOT / "src")

        failures = []
        for filename in OFFLINE_VERIFIERS:
            script = ROOT / "learning-examples" / filename
            result = subprocess.run(
                [sys.executable, str(script)],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            if result.returncode != 0:
                failures.append(
                    f"{filename} exited {result.returncode}\n"
                    f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
                )

        self.assertFalse(failures, "\n\n".join(failures))


if __name__ == "__main__":
    unittest.main()
