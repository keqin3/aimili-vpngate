"""Sticky connections and geographically constrained failover selection."""
from __future__ import annotations

import math
from typing import Any


def country(node: dict[str, Any]) -> str:
    code = str(node.get("geo_country_short") or node.get("country_short") or "").strip().upper()
    if len(code) == 2 and code.isalpha():
        return code
    return str(node.get("country") or "").strip().casefold()


def coords(node: dict[str, Any]) -> tuple[float, float] | None:
    try:
        lat, lon = float(node["geo_lat"]), float(node["geo_lon"])
        if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon
    except (KeyError, TypeError, ValueError):
        pass
    return None


def distance_km(a: dict[str, Any], b: dict[str, Any]) -> float:
    p, q = coords(a), coords(b)
    if p is None or q is None:
        return math.inf
    lat1, lon1, lat2, lon2 = map(math.radians, (*p, *q))
    hav = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 6371 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, hav))))


def nearby_candidates(nodes: list[dict[str, Any]], reference: dict[str, Any] | None,
                      excluded: set[str]) -> list[dict[str, Any]]:
    """Country is a hard boundary, proximity a best-effort ranking.

    Unknown countries are not assumed to be nearby. Missing coordinates fall
    back to region/city and measured latency, never numerical IP closeness.
    """
    ref = reference or {}
    target = country(ref)
    candidates = [n for n in nodes if n.get("probe_status") == "available"
                  and str(n.get("id")) not in excluded
                  and (not reference or (target and country(n) == target))
                  and (not reference or not (n.get("exit_ip") or n.get("ip"))
                       or (n.get("exit_ip") or n.get("ip")) != (ref.get("exit_ip") or ref.get("ip")))]
    def same(field: str, node: dict[str, Any]) -> bool:
        value = str(ref.get(field) or "").strip().casefold()
        return bool(value and value == str(node.get(field) or "").strip().casefold())
    def key(node: dict[str, Any]) -> tuple[Any, ...]:
        location = str(ref.get("location") or "").strip().casefold()
        same_location = bool(location and location == str(node.get("location") or "").strip().casefold())
        return (0 if same("geo_city", node) else 1,
                0 if same("geo_region", node) or same_location else 1,
                distance_km(ref, node),
                float(node.get("latency_ms") or 999999),
                str(node.get("id") or ""))
    return sorted(candidates, key=key)


def interval_due(config: dict[str, Any], state: dict[str, Any], now: float) -> bool:
    if config.get("switch_policy", "sticky") != "interval" or not config.get("connection_enabled", True):
        return False
    if config.get("routing_mode") == "fixed_ip":
        return False
    connected = float(state.get("connection_started_at") or 0)
    started = max(connected, float(state.get("rotation_base_at") or 0))
    interval = int(config.get("rotation_interval_seconds") or 3600)
    return connected > 0 and now >= started + interval
