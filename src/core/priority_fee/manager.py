from time import monotonic

from solders.pubkey import Pubkey

from core.client import SolanaClient
from core.priority_fee.dynamic_fee import DynamicPriorityFee
from core.priority_fee.fixed_fee import FixedPriorityFee
from core.priority_fee.strategy import (
    FeeEstimator,
    PriorityFeeCache,
    PriorityFeeMode,
    PriorityFeeSelection,
)
from utils.logger import get_logger

logger = get_logger(__name__)


class PriorityFeeManager:
    """Manager for priority fee calculation and validation."""

    def __init__(
        self,
        client: SolanaClient,
        enable_dynamic_fee: bool,
        enable_fixed_fee: bool,
        fixed_fee: int,
        extra_fee: float,
        hard_cap: int,
        strategy: str | PriorityFeeMode | None = None,
        cache_ttl_seconds: float = 5.0,
        refresh_interval_seconds: float = 2.0,
        provider_estimator: FeeEstimator | None = None,
    ):
        """
        Initialize the priority fee manager.

        Args:
            client: Solana RPC client for dynamic fee calculation.
            enable_dynamic_fee: Whether to enable dynamic fee calculation.
            enable_fixed_fee: Whether to enable fixed fee.
            fixed_fee: Fixed priority fee in microlamports.
            extra_fee: Percentage increase to apply to the base fee.
            hard_cap: Maximum allowed priority fee in microlamports.
        """
        self.client = client
        self.enable_dynamic_fee = enable_dynamic_fee
        self.enable_fixed_fee = enable_fixed_fee
        self.fixed_fee = fixed_fee
        self.extra_fee = extra_fee
        self.hard_cap = hard_cap
        self.mode = self._resolve_mode(strategy)

        # Initialize plugins
        self.dynamic_fee_plugin = DynamicPriorityFee(client)
        self.fixed_fee_plugin = FixedPriorityFee(fixed_fee)
        if (
            self.mode == PriorityFeeMode.PROVIDER_ESTIMATED
            and provider_estimator is None
        ):
            raise ValueError(
                "provider_estimated priority fees require a provider estimator"
            )
        estimator = (
            provider_estimator
            if self.mode == PriorityFeeMode.PROVIDER_ESTIMATED
            and provider_estimator is not None
            else self.dynamic_fee_plugin.get_priority_fee
        )
        source = (
            "provider"
            if self.mode == PriorityFeeMode.PROVIDER_ESTIMATED
            else "solana_getRecentPrioritizationFees"
        )
        self.cache = PriorityFeeCache(
            estimator,
            source=source,
            ttl_seconds=cache_ttl_seconds,
            refresh_interval_seconds=refresh_interval_seconds,
        )
        self.last_selection: PriorityFeeSelection | None = None

    async def calculate_priority_fee(
        self, accounts: list[Pubkey] | None = None
    ) -> int | None:
        """
        Calculate the priority fee based on the configuration.

        Args:
            accounts: List of accounts to consider for dynamic fee calculation.
                     If None, the fee is calculated without specific account constraints.

        Returns:
            Optional[int]: Calculated priority fee in microlamports, or None if no fee should be applied.
        """
        base_fee = await self._get_base_fee(accounts)
        if base_fee is None:
            return None

        # Apply extra fee (percentage increase)
        final_fee = int(base_fee * (1 + self.extra_fee))

        # Enforce hard cap
        if final_fee > self.hard_cap:
            logger.warning(
                f"Calculated priority fee {final_fee} exceeds hard cap {self.hard_cap}. Applying hard cap."
            )
            final_fee = self.hard_cap

        source = self.last_selection.source if self.last_selection else self.mode.value
        estimate = (
            self.last_selection.estimated_micro_lamports_per_cu
            if self.last_selection
            else base_fee
        )
        age = self.last_selection.estimate_age_ms if self.last_selection else 0.0
        latency = (
            self.last_selection.estimation_latency_ms if self.last_selection else None
        )
        self.last_selection = PriorityFeeSelection(
            final_fee,
            estimate,
            source,
            age,
            latency,
            self.last_selection.selected_at_mono
            if self.last_selection
            else monotonic(),
        )
        return final_fee

    async def _get_base_fee(self, accounts: list[Pubkey] | None = None) -> int | None:
        """
        Determine the base fee based on the configuration.

        Returns:
            Optional[int]: Base fee in microlamports, or None if no fee should be applied.
        """
        if self.mode == PriorityFeeMode.DISABLED:
            self.last_selection = PriorityFeeSelection(
                None, None, "disabled", None, None, monotonic()
            )
            return None
        if self.mode == PriorityFeeMode.FIXED:
            value = await self.fixed_fee_plugin.get_priority_fee()
            self.last_selection = PriorityFeeSelection(
                value, value, "fixed", 0.0, 0.0, monotonic()
            )
            return value
        if self.mode in {
            PriorityFeeMode.CACHED_DYNAMIC,
            PriorityFeeMode.PERIODIC_DYNAMIC,
            PriorityFeeMode.PROVIDER_ESTIMATED,
        }:
            selection = await self.cache.get(accounts)
            self.last_selection = selection
            if selection is not None:
                return selection.selected_micro_lamports_per_cu

        # Compatibility: synchronous dynamic selection remains unchanged.
        if self.enable_dynamic_fee:
            dynamic_fee = await self.dynamic_fee_plugin.get_priority_fee(accounts)
            if dynamic_fee is not None:
                self.last_selection = PriorityFeeSelection(
                    dynamic_fee,
                    dynamic_fee,
                    "solana_getRecentPrioritizationFees",
                    0.0,
                    None,
                    monotonic(),
                )
                return dynamic_fee

        # Fall back to fixed fee if enabled
        if self.enable_fixed_fee:
            value = await self.fixed_fee_plugin.get_priority_fee()
            self.last_selection = PriorityFeeSelection(
                value, value, "fixed_fallback", 0.0, 0.0, monotonic()
            )
            return value

        # No priority fee if both are disabled
        return None

    async def start(self, accounts: list[Pubkey] | None = None) -> None:
        """Pre-warm cached strategies before token detection starts."""
        if self.mode in {
            PriorityFeeMode.CACHED_DYNAMIC,
            PriorityFeeMode.PERIODIC_DYNAMIC,
            PriorityFeeMode.PROVIDER_ESTIMATED,
        }:
            try:
                await self.cache.refresh(accounts)
            except Exception:  # noqa: BLE001 - a later selection can retry
                logger.warning(
                    "Could not pre-warm priority fee estimate", exc_info=True
                )
        if self.mode == PriorityFeeMode.PERIODIC_DYNAMIC:
            await self.cache.start_periodic(accounts)

    async def close(self) -> None:
        await self.cache.close()

    def _resolve_mode(self, strategy: str | PriorityFeeMode | None) -> PriorityFeeMode:
        if strategy is not None:
            return PriorityFeeMode(strategy)
        if self.enable_dynamic_fee:
            return PriorityFeeMode.DYNAMIC
        if self.enable_fixed_fee:
            return PriorityFeeMode.FIXED
        return PriorityFeeMode.DISABLED
