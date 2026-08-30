"""Vendor-specific configuration around the generic Yellowstone listener."""

# Provider constructors mirror the generic listener's explicit connection inputs.
# ruff: noqa: PLR0913

from __future__ import annotations

from typing import TYPE_CHECKING

from monitoring.universal_geyser_listener import UniversalGeyserListener

if TYPE_CHECKING:
    from interfaces.core import Platform


class RabbitStreamListener(UniversalGeyserListener):
    """Shyft RabbitStream, documented as Yellowstone gRPC wire-compatible."""

    def __init__(
        self,
        endpoint: str,
        api_token: str,
        *,
        region: str | None = None,
        auth_type: str = "x-token",
        platforms: list[Platform] | None = None,
        commitment: str = "processed",
    ) -> None:
        super().__init__(
            endpoint,
            api_token,
            auth_type,
            platforms,
            commitment=commitment,
            source_name="rabbitstream",
            source_region=region,
        )


class TritonRiptideListener(UniversalGeyserListener):
    """Triton Riptide/Dragon's Mouth through generic Yellowstone protobufs."""

    def __init__(
        self,
        endpoint: str,
        api_token: str,
        *,
        region: str | None = None,
        auth_type: str = "x-token",
        platforms: list[Platform] | None = None,
        commitment: str = "processed",
    ) -> None:
        super().__init__(
            endpoint,
            api_token,
            auth_type,
            platforms,
            commitment=commitment,
            source_name="triton_riptide",
            source_region=region,
        )
