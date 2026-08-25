from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from interfaces.core import Platform  # noqa: E402
from utils.idl_manager import get_idl_manager  # noqa: E402


class IdlFeeDecodingTests(unittest.TestCase):
    def test_u128_and_vector_fee_tier_decode(self):
        parser = get_idl_manager().get_parser(Platform.PUMP_FUN)
        discriminator = bytes([143, 52, 146, 187, 219, 123, 76, 155])
        payload = bytearray(discriminator)
        payload += struct.pack("<B", 254)
        payload += bytes(range(32))
        payload += struct.pack("<QQQ", 0, 100, 50)
        payload += struct.pack("<I", 1)
        payload += (123_456_789_012_345_678_901).to_bytes(16, "little")
        payload += struct.pack("<QQQ", 0, 80, 20)
        payload += struct.pack("<I", 0)
        decoded = parser.decode_account_data(bytes(payload), "FeeConfig")
        self.assertEqual(decoded["bump"], 254)
        self.assertEqual(decoded["flat_fees"]["protocol_fee_bps"], 100)
        self.assertEqual(len(decoded["fee_tiers"]), 1)
        self.assertEqual(
            decoded["fee_tiers"][0]["market_cap_lamports_threshold"],
            123_456_789_012_345_678_901,
        )
        self.assertEqual(decoded["fee_tiers"][0]["fees"]["creator_fee_bps"], 20)

    def test_malformed_vector_length_is_rejected(self):
        parser = get_idl_manager().get_parser(Platform.PUMP_FUN)
        discriminator = bytes([143, 52, 146, 187, 219, 123, 76, 155])
        payload = discriminator + bytes(1 + 32 + 24) + struct.pack("<I", 999)
        self.assertIsNone(parser.decode_account_data(payload, "FeeConfig"))


if __name__ == "__main__":
    unittest.main()
