"""Final small compatibility fix for the live hotel-detail contract."""

from __future__ import annotations

from datetime import date
from typing import Any

from .refined import HilTools as RefinedHilTools


class HilTools(RefinedHilTools):
    async def get_stay_detail(
        self,
        *,
        hotel_id: str,
        check_in: date | str,
        check_out: date | str,
        adults: int = 1,
        max_rooms: int = 10,
        max_rates_per_room: int = 5,
    ) -> dict[str, Any]:
        provider_hotel_id: str | int = int(hotel_id) if str(hotel_id).isdigit() else hotel_id
        return await super().get_stay_detail(
            hotel_id=provider_hotel_id,
            check_in=check_in,
            check_out=check_out,
            adults=adults,
            max_rooms=max_rooms,
            max_rates_per_room=max_rates_per_room,
        )
