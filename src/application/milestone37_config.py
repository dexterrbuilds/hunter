"""Safe parsing for wallet tracking and launch/fleet configuration."""

# Config validation deliberately emits field-specific ValueErrors.
# ruff: noqa: C901, FBT001, FBT003, PLR2004, TRY003, TRY004

from __future__ import annotations

from decimal import Decimal

from solders.pubkey import Pubkey

from core.pubkeys import WSOL_MINT
from domain.amounts import (
    BasisPoints,
    QuoteAmountRaw,
    RoundingDirection,
)
from domain.wallet_tracking import (
    CopySizingMode,
    DuplicatePolicy,
    TrackedWallet,
    TrackedWalletAction,
    WalletTrackingConfig,
)


def wallet_tracking_config_from_dict(value: dict | None) -> WalletTrackingConfig:
    """Parse disabled-by-default tracking settings with exact SOL amounts."""
    if value is None:
        return WalletTrackingConfig()
    if not isinstance(value, dict):
        raise ValueError("wallet_tracking must be a mapping")
    enabled = _bool(value, "enabled", False)
    policy = DuplicatePolicy(value.get("duplicate_policy", "ignore_existing_position"))
    wallets = []
    for index, item in enumerate(value.get("wallets", [])):
        if not isinstance(item, dict):
            raise ValueError(f"wallet_tracking.wallets[{index}] must be a mapping")
        try:
            address = Pubkey.from_string(item["address"])
        except (KeyError, ValueError, TypeError) as error:
            raise ValueError(
                f"wallet_tracking.wallets[{index}].address is invalid"
            ) from error
        wallets.append(
            TrackedWallet(
                address=address,
                label=item.get("label"),
                watch_create=_bool(item, "watch_create", True),
                watch_buy=_bool(item, "watch_buy", True),
                create_action=_action(item.get("create_action"), "create_action"),
                copy_action=_action(item.get("copy_action"), "copy_action"),
            )
        )
    if enabled and not wallets:
        raise ValueError("enabled wallet tracking requires at least one wallet")
    return WalletTrackingConfig(
        enabled=enabled,
        duplicate_policy=policy,
        wallets=tuple(wallets),
        maximum_pending_events=_positive_int(value, "maximum_pending_events", 256),
        decoder_workers=_positive_int(value, "decoder_workers", 2),
    )


def validate_wallet_fleet_config(value: dict | None) -> None:
    """Validate secret references without resolving or logging their values."""
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError("wallet_fleet must be a mapping")
    enabled = _bool(value, "enabled", False)
    wallets = value.get("wallets", [])
    if not isinstance(wallets, list):
        raise ValueError("wallet_fleet.wallets must be a list")
    if enabled and not wallets:
        raise ValueError("enabled wallet fleet requires wallets")
    ids: set[str] = set()
    for index, wallet in enumerate(wallets):
        if not isinstance(wallet, dict):
            raise ValueError(f"wallet_fleet.wallets[{index}] must be a mapping")
        wallet_id = wallet.get("id")
        signer = wallet.get("signer")
        if not isinstance(wallet_id, str) or not wallet_id.strip():
            raise ValueError("fleet wallet id cannot be empty")
        if wallet_id in ids:
            raise ValueError("fleet wallet IDs must be unique")
        ids.add(wallet_id)
        if not isinstance(signer, str) or not signer.startswith("env:"):
            raise ValueError("fleet signer must be an env: reference")
        if len(signer) <= 4:
            raise ValueError("fleet signer environment name cannot be empty")
    launch = value.get("launch", {})
    if not isinstance(launch, dict):
        raise ValueError("wallet_fleet.launch must be a mapping")
    if enabled and not _bool(launch, "risk_enforced", False):
        raise ValueError("enabled wallet fleet requires launch.risk_enforced: true")


def validate_token_launch_config(
    value: dict | None, *, wallet_fleet_enabled: bool
) -> None:
    """Validate disabled-by-default launch orchestration selection."""
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError("token_launch must be a mapping")
    enabled = _bool(value, "enabled", False)
    execution = value.get("execution", {})
    exit_policy = value.get("exit", {})
    if not isinstance(execution, dict) or not isinstance(exit_policy, dict):
        raise ValueError("token_launch execution and exit must be mappings")
    mode = execution.get("mode", "bundle")
    if mode not in {"bundle", "parallel_fast", "sequential"}:
        raise ValueError("token_launch.execution.mode is unsupported")
    exit_type = exit_policy.get("type", "manual")
    if exit_type not in {
        "manual",
        "profit_target",
        "time_based",
        "take_profit",
        "stop_loss",
    }:
        raise ValueError("token_launch.exit.type is unsupported")
    if enabled and not wallet_fleet_enabled:
        raise ValueError("enabled token launch requires wallet_fleet.enabled: true")


def _action(value: object, label: str) -> TrackedWalletAction:
    if value is None:
        return TrackedWalletAction(enabled=False)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    enabled = _bool(value, "enabled", False)
    mode = CopySizingMode(value.get("sizing_mode", "fixed"))
    fixed = None
    if "buy_amount_sol" in value:
        raw = QuoteAmountRaw.from_decimal(
            Decimal(str(value["buy_amount_sol"])),
            mint=WSOL_MINT,
            decimals=9,
            rounding=RoundingDirection.DOWN,
        )
        fixed = raw
    percentage = (
        BasisPoints(_nonnegative_int(value, "percentage_bps", 0))
        if "percentage_bps" in value
        else None
    )
    return TrackedWalletAction(
        enabled=enabled,
        sizing_mode=mode,
        fixed_quote_amount=fixed,
        percentage_bps=percentage,
        slippage=BasisPoints(_nonnegative_int(value, "slippage_bps", 100)),
    )


def _bool(value: dict, key: str, default: bool) -> bool:
    result = value.get(key, default)
    if not isinstance(result, bool):
        raise ValueError(f"{key} must be true or false")
    return result


def _positive_int(value: dict, key: str, default: int) -> int:
    result = value.get(key, default)
    if isinstance(result, bool) or not isinstance(result, int) or result <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return result


def _nonnegative_int(value: dict, key: str, default: int) -> int:
    result = value.get(key, default)
    if isinstance(result, bool) or not isinstance(result, int) or result < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return result
