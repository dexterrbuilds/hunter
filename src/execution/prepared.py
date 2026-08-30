"""Construct, sign, and serialize exactly once for each economic variant."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from execution.ports import SignedTransaction, Signer, UnsignedTransaction


@dataclass(frozen=True, slots=True)
class PreparedExecutionVariant:
    """One immutable signed wire identity reusable across compatible transports."""

    variant: str
    transaction: SignedTransaction


class ExecutionVariantPreparer:
    """Memoize one signer invocation and one serialized wire result per variant."""

    def __init__(self, signer: Signer) -> None:
        self.signer = signer
        self._prepared: dict[str, PreparedExecutionVariant] = {}

    async def prepare(
        self, variant: str, unsigned: UnsignedTransaction
    ) -> PreparedExecutionVariant:
        existing = self._prepared.get(variant)
        if existing is not None:
            return existing
        signed = await self.signer.sign(unsigned)
        prepared = PreparedExecutionVariant(variant, signed)
        self._prepared[variant] = prepared
        return prepared
