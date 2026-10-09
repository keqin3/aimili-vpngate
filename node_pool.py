"""Bounded, stable node selection. No network calls or fabricated IP classes."""
from __future__ import annotations

import ipaddress
from typing import Any


def ip_keys(node: dict[str, Any]) -> set[str]:
    keys = set()
    for field in ("exit_ip", "ip", "remote_host"):
        value = str(node.get(field) or "").strip().lower().strip("[]")
        if not value:
            continue
        try:
            value = str(ipaddress.ip_address(value))
        except ValueError:
            value = value.rstrip(".")
        keys.add(value)
    return keys


def failed(node: dict[str, Any], threshold: int) -> bool:
    message = str(node.get("probe_message") or "").lower()
    if any(token in message for token in (
        "err_ovpn_cmd_not_found", "err_ovpn_permission_denied", "err_ovpn_tun_not_available",
        "cannot open tun/tap dev", "cannot allocate tun", "no such file or directory",
    )):
        return False  # A broken local runtime is not proof that a remote IP died.
    return (node.get("probe_status") == "unavailable"
            and int(node.get("consecutive_failures") or 0) >= threshold)


def select_pool(current: list[dict[str, Any]], candidates: list[dict[str, Any]],
                history: set[str], protected: set[str], residential: int = 200,
                hosting: int = 50, pending: int = 60, rotate: bool = False,
                failure_threshold: int = 2) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Prefer existing nodes; retirement never silently reintroduces a seen IP.

    Active/favourite/fixed nodes are explicit exceptions to quota/rotation.
    Pending/unknown/mobile nodes never count toward the residential target.
    """
    kept: list[dict[str, Any]] = []
    keys: set[str] = set()
    ids: set[str] = set()
    counts = {"residential": 0, "hosting": 0, "pending": 0}
    current_ids = {str(n.get("id")) for n in current}
    protected_nodes = [n for n in current if str(n.get("id")) in protected or n.get("active")]
    ordinary = [] if rotate else [n for n in current if n not in protected_nodes]
    for node in [*protected_nodes, *ordinary, *candidates]:
        node_id = str(node.get("id") or "")
        aliases = ip_keys(node)
        if not node_id or not aliases or node_id in ids:
            continue
        is_protected = node_id in protected or bool(node.get("active"))
        # Only the currently retained batch may reuse history in ordinary refill.
        is_current = node_id in current_ids and not rotate
        if not is_protected and (aliases & keys or (not is_current and aliases & history)):
            continue
        if not is_protected and failed(node, failure_threshold):
            continue
        category = str(node.get("ip_type") or "unknown").lower()
        if node.get("node_kind") == "public_proxy" and not node.get("exit_ip"):
            node = dict(node, source_ip_type_hint=category, ip_type="unknown")
            category = "unknown"
        bucket = category if category in ("residential", "hosting") else "pending"
        if not is_protected:
            if bucket == "pending" and int(node.get("pool_classification_attempts") or 0) >= 3:
                continue
            if category in ("mobile", "proxy"):
                continue
            if counts[bucket] >= {"residential": residential, "hosting": hosting, "pending": pending}[bucket]:
                continue
        kept.append(node)
        ids.add(node_id)
        keys.update(aliases)
        counts[bucket] += 1
    if counts["residential"] >= residential and counts["hosting"] >= hosting:
        kept = [n for n in kept if n.get("ip_type") in {"residential", "hosting"}
                or str(n.get("id")) in protected or n.get("active")]
        counts["pending"] = sum(n.get("ip_type") not in {"residential", "hosting"} for n in kept)
    counts["residential_available"] = sum(n.get("ip_type") == "residential" and n.get("probe_status") == "available" for n in kept)
    counts["hosting_available"] = sum(n.get("ip_type") == "hosting" and n.get("probe_status") == "available" for n in kept)
    counts["residential_missing"] = max(0, residential - counts["residential"])
    counts["hosting_missing"] = max(0, hosting - counts["hosting"])
    return kept, counts
