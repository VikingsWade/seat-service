from __future__ import annotations

from typing import Annotated, Optional

from pydantic import BaseModel, Field, StrictInt, StringConstraints, field_validator

from .auth import USER_ID_PATTERN

SeatLabel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
IdempotencyKey = Annotated[str, StringConstraints(min_length=1, max_length=200)]


def _require_unique(seats: list[str]) -> list[str]:
    if len(set(seats)) != len(seats):
        raise ValueError("seats must not contain duplicates")
    return seats


class TokenRequest(BaseModel):
    user_id: str = Field(pattern=USER_ID_PATTERN)


class CreateShowRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    seats: list[SeatLabel] = Field(min_length=1)
    price_paise: StrictInt = Field(ge=0)
    per_user_limit: Optional[Annotated[StrictInt, Field(ge=1, le=100)]] = None

    @field_validator("seats")
    @classmethod
    def _unique_seats(cls, value: list[str]) -> list[str]:
        return _require_unique(value)


class ReserveRequest(BaseModel):
    seats: list[SeatLabel] = Field(min_length=1)
    idempotency_key: Optional[IdempotencyKey] = None

    @field_validator("seats")
    @classmethod
    def _unique_seats(cls, value: list[str]) -> list[str]:
        return _require_unique(value)
