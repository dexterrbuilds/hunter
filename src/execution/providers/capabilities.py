"""Provider capabilities used to validate safe execution routing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TransportCapability(StrEnum):
    """Normalized delivery features exposed by a provider adapter."""

    STANDARD_RPC = "standard_rpc"
    SWQOS = "swqos"
    JITO = "jito"
    SENDER_MAX = "sender_max"
    DIRECT_LEADER = "direct_leader"
    TIPPED_VARIANT = "tipped_variant"
    SAME_SIGNATURE_COMPATIBLE = "same_signature_compatible"
    MULTI_TRANSACTION_BUNDLE = "multi_transaction_bundle"


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Message requirements that determine whether transports may be raced."""

    accepts_standard_signed_tx: bool
    supports_same_signature_race: bool
    requires_tip: bool = False
    requires_priority_fee: bool = False
    execution_variants: frozenset[str] = frozenset({"standard"})
    features: frozenset[TransportCapability] = frozenset()
    maximum_bundle_transactions: int | None = None

    def accepts_variant(self, variant: str) -> bool:
        """Return whether the already-signed message variant is accepted."""
        return variant in self.execution_variants

    def race_compatible_with(
        self, other: ProviderCapabilities, *, variant: str
    ) -> bool:
        """Require both transports to accept the exact same signed variant."""
        return (
            self.supports_same_signature_race
            and other.supports_same_signature_race
            and self.accepts_variant(variant)
            and other.accepts_variant(variant)
        )


STANDARD_CAPABILITIES = ProviderCapabilities(
    accepts_standard_signed_tx=True,
    supports_same_signature_race=True,
    execution_variants=frozenset(
        {"standard", "jito_tipped", "helius_sender_tipped", "sender_max_tipped"}
    ),
    features=frozenset(
        {
            TransportCapability.STANDARD_RPC,
            TransportCapability.SAME_SIGNATURE_COMPATIBLE,
        }
    ),
)

SWQOS_CAPABILITIES = ProviderCapabilities(
    accepts_standard_signed_tx=True,
    supports_same_signature_race=True,
    execution_variants=frozenset(
        {"standard", "jito_tipped", "helius_sender_tipped", "sender_max_tipped"}
    ),
    features=frozenset(
        {
            TransportCapability.SWQOS,
            TransportCapability.DIRECT_LEADER,
            TransportCapability.SAME_SIGNATURE_COMPATIBLE,
        }
    ),
)

HELIUS_SENDER_CAPABILITIES = ProviderCapabilities(
    accepts_standard_signed_tx=False,
    supports_same_signature_race=True,
    requires_tip=True,
    requires_priority_fee=True,
    execution_variants=frozenset({"helius_sender_tipped", "jito_tipped"}),
    features=frozenset(
        {
            TransportCapability.TIPPED_VARIANT,
            TransportCapability.SAME_SIGNATURE_COMPATIBLE,
        }
    ),
)

HELIUS_SENDER_MAX_CAPABILITIES = ProviderCapabilities(
    accepts_standard_signed_tx=False,
    supports_same_signature_race=True,
    requires_tip=True,
    requires_priority_fee=True,
    execution_variants=frozenset({"sender_max_tipped"}),
    features=frozenset(
        {
            TransportCapability.SENDER_MAX,
            TransportCapability.TIPPED_VARIANT,
            TransportCapability.SAME_SIGNATURE_COMPATIBLE,
        }
    ),
)

JITO_CAPABILITIES = ProviderCapabilities(
    accepts_standard_signed_tx=True,
    supports_same_signature_race=True,
    execution_variants=frozenset({"standard", "jito_tipped", "helius_sender_tipped"}),
    features=frozenset(
        {
            TransportCapability.JITO,
            TransportCapability.DIRECT_LEADER,
            TransportCapability.SAME_SIGNATURE_COMPATIBLE,
            TransportCapability.MULTI_TRANSACTION_BUNDLE,
        }
    ),
    maximum_bundle_transactions=5,
)
