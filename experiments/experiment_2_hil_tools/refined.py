"""Small output refinements used by the executable experiment runner."""

from __future__ import annotations

from typing import Any

from .tools import HilTools as BaseHilTools


class HilTools(BaseHilTools):
    """Keep supplier evidence raw while making tool output decision-sized."""

    async def search_stays(self, **kwargs: Any) -> dict[str, Any]:
        result = await super().search_stays(**kwargs)
        candidates = result.get("data", {}).get("candidates", []) if isinstance(result.get("data"), dict) else []
        for candidate in candidates:
            if not candidate.get("hotel_id") and str(candidate.get("candidate_id", "")).startswith("hotel:"):
                candidate["hotel_id"] = str(candidate["candidate_id"]).split(":", 1)[1]
        return result

    async def resolve_place(self, **kwargs: Any) -> dict[str, Any]:
        result = await super().resolve_place(**kwargs)
        candidates = result.get("data", {}).get("candidates", []) if isinstance(result.get("data"), dict) else []
        for candidate in candidates:
            detail = candidate.get("detail")
            if isinstance(detail, dict):
                candidate["detail"] = {
                    key: detail.get(key)
                    for key in ("id", "name", "address", "location", "type", "business_area", "rating", "tel", "entr_location")
                }
            geocode = candidate.get("geocode")
            if isinstance(geocode, dict):
                candidate["geocode"] = {
                    key: geocode.get(key)
                    for key in ("formatted_address", "province", "city", "district", "adcode", "location", "level")
                }
        return result

    async def calculate_route(self, **kwargs: Any) -> dict[str, Any]:
        result = await super().calculate_route(**kwargs)
        paths = result.get("data", {}).get("paths", []) if isinstance(result.get("data"), dict) else []
        for path in paths:
            if path.get("duration_seconds") is None and isinstance(path.get("cost"), dict):
                try:
                    path["duration_seconds"] = int(path["cost"]["duration"])
                except (KeyError, TypeError, ValueError):
                    pass
        return result
