"""Tests for Hunter's centralized logging redaction."""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from queue import SimpleQueue

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from utils.logger import RedactingFormatter, RedactingQueueHandler  # noqa: E402
from utils.redaction import (  # noqa: E402
    REDACTED,
    clear_registered_secrets,
    endpoint_identifier,
    register_config_secrets,
    register_secret,
    sanitize_text,
)


class SecretRedactionTests(unittest.TestCase):
    """Prove representative credentials cannot survive formatting."""

    def setUp(self) -> None:
        clear_registered_secrets()

    def tearDown(self) -> None:
        clear_registered_secrets()

    def test_redacts_url_userinfo_query_and_path_key(self) -> None:
        user = "rpc-user"
        password = "rpc-password"
        query_key = "query-secret-123"
        path_key = "opaqueProviderApiKey123456789"
        raw = (
            f"https://{user}:{password}@rpc.example.com/v2/{path_key}"
            f"?api-key={query_key}&commitment=processed"
        )

        sanitized = sanitize_text(f"RPC failed at {raw}")

        for secret in (user, password, query_key, path_key):
            self.assertNotIn(secret, sanitized)
        self.assertIn("rpc.example.com", sanitized)
        self.assertIn("commitment=processed", sanitized)

    def test_redacts_wallet_geyser_and_telegram_values(self) -> None:
        wallet = "3KMfFakeBase58WalletSecretThatMustNeverReachLogs"
        geyser = "geyser-private-token"
        telegram = "123456789:AAExampleTelegramToken_0123456789abcdef"
        register_secret(wallet)

        sanitized = sanitize_text(
            f"wallet_secret={wallet} api_token={geyser} telegram={telegram}"
        )

        self.assertNotIn(wallet, sanitized)
        self.assertNotIn(geyser, sanitized)
        self.assertNotIn(telegram, sanitized)
        self.assertGreaterEqual(sanitized.count(REDACTED), 3)

    def test_registers_known_config_secrets(self) -> None:
        private_key = "base58-private-key-value"
        rpc_url = "https://rpc.example.com/v2/configured-path-api-key"
        geyser_token = "configured-geyser-token"
        register_config_secrets(
            {
                "private_key": private_key,
                "rpc_endpoint": rpc_url,
                "geyser": {"api_token": geyser_token},
            }
        )

        sanitized = sanitize_text(f"{private_key} {rpc_url} {geyser_token}")

        self.assertEqual(sanitized, f"{REDACTED} {REDACTED} {REDACTED}")

    def test_registers_jito_and_geyser_auth_headers(self) -> None:
        register_config_secrets(
            {
                "headers": {
                    "x-jito-auth": "private-jito-uuid",
                    "x-token": "private-geyser-token",
                }
            }
        )
        sanitized = sanitize_text("private-jito-uuid private-geyser-token")
        self.assertEqual(sanitized, f"{REDACTED} {REDACTED}")

    def test_formatter_redacts_exception_text(self) -> None:
        secret = "exception-secret-token"
        register_secret(secret)
        formatter = RedactingFormatter("%(levelname)s %(message)s")
        try:
            raise RuntimeError(f"transport failed for {secret}")
        except RuntimeError:
            record = logging.LogRecord(
                name="hunter.test",
                level=logging.ERROR,
                pathname=__file__,
                lineno=1,
                msg="RPC exception",
                args=(),
                exc_info=sys.exc_info(),
            )

        rendered = formatter.format(record)

        self.assertNotIn(secret, rendered)
        self.assertIn(REDACTED, rendered)

    def test_queue_handoff_redacts_message_without_formatting_exception(self) -> None:
        secret = "queued-secret-token"
        register_secret(secret)
        try:
            raise RuntimeError(f"late exception formatting {secret}")
        except RuntimeError:
            record = logging.LogRecord(
                name="hunter.test",
                level=logging.ERROR,
                pathname=__file__,
                lineno=1,
                msg="provider=%s",
                args=(secret,),
                exc_info=sys.exc_info(),
            )

        prepared = RedactingQueueHandler(SimpleQueue()).prepare(record)

        self.assertEqual(prepared.msg, f"provider={REDACTED}")
        self.assertIsNotNone(prepared.exc_info)
        self.assertIsNone(prepared.exc_text)

    def test_endpoint_identifier_never_contains_credentials(self) -> None:
        endpoint = "https://user:password@rpc.example.com/v2/path-secret?api-key=q"

        identifier = endpoint_identifier(endpoint)

        self.assertTrue(identifier.startswith("https://rpc.example.com#"))
        for secret in ("user", "password", "path-secret", "api-key", "q"):
            self.assertNotIn(secret, identifier)

    def test_endpoint_identifier_is_stable_across_credential_rotation(self) -> None:
        first = endpoint_identifier(
            "https://user:password@rpc.example.com/v2?api-key=first-secret"
        )
        second = endpoint_identifier(
            "https://other:new-password@rpc.example.com/v2?api-key=next-secret"
        )

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
