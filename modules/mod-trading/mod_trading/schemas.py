"""Trading domain records. Money and quantities are Decimal; times are timezone-aware."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from trading_risk import Side

Price = Annotated[Decimal, Field(gt=0)]
Quantity = Annotated[Decimal, Field(gt=0)]
Instrument = Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9.\-]+$")]

__all__ = [
    "Bar",
    "Fill",
    "Order",
    "OrderIntent",
    "OrderRequest",
    "OrderStatus",
    "OrderType",
    "Quote",
    "Side",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Quote(_Frozen):
    instrument: Instrument
    bid: Price
    ask: Price
    ts: AwareDatetime

    @model_validator(mode="after")
    def _not_crossed(self) -> Quote:
        if self.bid > self.ask:
            raise ValueError("bid above ask")
        return self

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


class Bar(_Frozen):
    instrument: Instrument
    ts: AwareDatetime = Field(description="bar close time")
    open: Price
    high: Price
    low: Price
    close: Price
    volume: Decimal = Field(default=Decimal(0), ge=0)

    @model_validator(mode="after")
    def _ohlc(self) -> Bar:
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("inconsistent OHLC")
        return self


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(StrEnum):
    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class OrderIntent(_Frozen):
    """What a strategy wants. It is only a proposal until the executor has run every check."""

    strategy: str = Field(min_length=1, max_length=60)
    instrument: Instrument
    side: Side
    quantity: Quantity
    order_type: OrderType = OrderType.MARKET
    limit_price: Price | None = None
    reason: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def _limit(self) -> OrderIntent:
        if (self.order_type is OrderType.LIMIT) != (self.limit_price is not None):
            raise ValueError("limit orders need a limit price, market orders none")
        return self


class OrderRequest(_Frozen):
    client_order_id: UUID = Field(default_factory=uuid4)
    instrument: Instrument
    side: Side
    quantity: Quantity
    order_type: OrderType
    limit_price: Price | None = None


class Fill(_Frozen):
    order_id: str
    instrument: Instrument
    side: Side
    quantity: Quantity
    price: Price
    fee: Decimal = Field(ge=0)
    ts: AwareDatetime


class Order(_Frozen):
    order_id: str
    request: OrderRequest
    status: OrderStatus
    filled_quantity: Decimal = Field(default=Decimal(0), ge=0)
    avg_price: Decimal | None = None
    reject_reason: str | None = Field(default=None, max_length=200)
    created_at: AwareDatetime
    updated_at: AwareDatetime
