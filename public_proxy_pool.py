#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
import ssl
import time
import urllib.parse
import urllib.request
from typing import Any


MAX_RESPONSE_BYTES = int(os.environ.get("PUBLIC_PROXY_SOURCE_MAX_BYTES", str(20 * 1024 * 1024)))
SOURCE_TIMEOUT_SECONDS = int(os.environ.get("PUBLIC_PROXY_SOURCE_TIMEOUT", "25"))
MAX_PER_SOURCE = int(os.environ.get("PUBLIC_PROXY_MAX_PER_SOURCE", "1500"))
PROBE_TIMEOUT_SECONDS = int(os.environ.get("PUBLIC_PROXY_PROBE_TIMEOUT", "10"))

SOURCE_URLS = {
    "m1noa": "https://raw.githubusercontent.com/M1noa/proxypool/output/proxies.json",
    "maximilianfeix": "https://raw.githubusercontent.com/maximilianfeix/free-proxy-list/main/proxies.json",
    "proxyscrape": "https://cdn.jsdelivr.net/gh/ProxyScrape/free-proxy-list@main/proxies/all/data.json",
    "stormsia": "https://stormsia.github.io/proxy-list/proxies.json",
    "hproxy": "https://hproxy.com/api/proxy-list?format=json&limit=1500",
    "databay_http": "https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/http.txt",
    "databay_socks5": "https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/socks5.txt",
    "geonode": "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc",
}
ALLOWED_SOURCE_HOSTS = {
    "raw.githubusercontent.com",
    "cdn.jsdelivr.net",
    "stormsia.github.io",
    "hproxy.com",
    "proxylist.geonode.com",
}

TEXT_SOURCE_PROTOCOLS = {
    "databay_http": "http",
    "databay_socks5": "socks5",
}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _first(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, "", []):
            return value
    return None


def normalize_protocol(value: Any) -> str:
    if isinstance(value, list):
        preferred = [str(item).lower() for item in value]
        for candidate in ("socks5", "http", "https"):
            if candidate in preferred:
                return "http" if candidate == "https" else candidate
        return ""
    protocol = str(value or "").strip().lower().replace("socks5h", "socks5")
    if protocol in {"http", "https", "connect"}:
        return "http"
    if protocol in {"socks5", "socks"}:
        return "socks5"
    return ""


def _parse_url_endpoint(value: Any) -> tuple[str, str, int]:
    text = str(value or "").strip()
    if not text:
        return "", "", 0
    if "://" not in text:
        text = "http://" + text
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        return "", "", 0
    return normalize_protocol(parsed.scheme), str(parsed.hostname or ""), int(parsed.port or 0)


def normalize_record(record: dict[str, Any], source: str) -> dict[str, Any] | None:
    protocol = normalize_protocol(_first(record, "protocol", "type", "scheme", "protocols"))
    host = str(_first(record, "host", "ip", "proxy_ip", "address") or "").strip()
    port = _int(_first(record, "port", "proxy_port"))
    if not host or not port or not protocol:
        url_protocol, url_host, url_port = _parse_url_endpoint(_first(record, "url", "proxy", "proxyUrl"))
        protocol = protocol or url_protocol
        host = host or url_host
        port = port or url_port
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        if not host or len(host) > 253 or any(ch.isspace() for ch in host):
            return None
    if protocol not in {"http", "socks5"} or not (1 <= port <= 65535):
        return None

    host = host.strip("[]").lower()
    country_code = str(_first(record, "country_code", "countryCode", "country") or "").upper()
    if len(country_code) != 2:
        country_code = ""
    country_name = str(_first(record, "country_name", "country") or country_code)
    isp = str(_first(record, "isp", "provider", "organization", "org", "as_name") or "")
    asn = str(_first(record, "asn", "as") or "")
    raw_type = str(_first(record, "ip_type", "proxy_type", "network_type") or "").lower()
    is_datacenter = bool(record.get("is_datacenter") or record.get("hosting"))
    if raw_type in {"residential", "isp"} and not is_datacenter:
        ip_type = "residential"
        confidence = "low"
    elif raw_type == "mobile":
        ip_type = "mobile"
        confidence = "low"
    elif raw_type in {"hosting", "datacenter", "data_center"} or is_datacenter:
        ip_type = "hosting"
        confidence = "medium"
    else:
        ip_type = "unknown"
        confidence = "low"
    endpoint_key = f"{protocol}|{host}|{port}"
    node_id = "px_" + hashlib.sha256(endpoint_key.encode("utf-8")).hexdigest()[:20]
    latency = _int(_first(record, "latency_ms", "latency", "timeout", "response_time"))
    if 0 < latency < 20 and "timeout" in record:
        latency = int(_float(record.get("timeout")) * 1000)
    return {
        "id": node_id,
        "node_kind": "public_proxy",
        "protocol": protocol,
        "proto": protocol,
        "ip": host,
        "remote_host": host,
        "remote_port": port,
        "port": port,
        "endpoint_key": endpoint_key,
        "sources": [source],
        "source": source,
        "country": country_name,
        "country_short": country_code,
        "geo_country_short": country_code,
        "owner": isp,
        "isp": isp,
        "asn": asn,
        "as_name": isp,
        "location": country_name,
        "ip_type": ip_type,
        "ip_type_confidence": confidence,
        "ip_type_reason": "source_asn_hint" if ip_type != "unknown" else "awaiting_exit_verification",
        "latency_ms": max(0, latency),
        "handshake_ms": 0,
        "uptime_percent": _float(_first(record, "uptime_percent", "uptime", "uptime_7d")),
        "probe_status": "not_checked",
        "probe_message": "等待本机真实代理握手检测",
        "probed_at": 0,
        "consecutive_failures": 0,
        "active": False,
    }


def _extract_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "proxies", "results", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            for nested_key in ("data", "proxies", "items"):
                nested = value.get(nested_key)
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
    return []


def fetch_json(url: str) -> Any:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_SOURCE_HOSTS:
        raise ValueError("public proxy source host is not allowed")
    request = urllib.request.Request(url, headers={"User-Agent": "AimiliVPN-PublicProxyPool/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=SOURCE_TIMEOUT_SECONDS) as response:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise ValueError("public proxy source response is too large")
            chunks.append(chunk)
    return json.loads(b"".join(chunks).decode("utf-8", errors="replace"))


def fetch_source_records(source: str, url: str) -> list[dict[str, Any]]:
    """Fetch one bounded source and convert text feeds to normal records."""
    if source not in TEXT_SOURCE_PROTOCOLS:
        return _extract_records(fetch_json(url))
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_SOURCE_HOSTS:
        raise ValueError("public proxy source host is not allowed")
    request = urllib.request.Request(url, headers={"User-Agent": "AimiliVPN-PublicProxyPool/1.0", "Accept": "text/plain"})
    with urllib.request.urlopen(request, timeout=SOURCE_TIMEOUT_SECONDS) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("public proxy source response is too large")
    records: list[dict[str, Any]] = []
    protocol = TEXT_SOURCE_PROTOCOLS[source]
    for line in raw.decode("utf-8", errors="replace").splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        records.append({"proxy": f"{protocol}://{value}", "protocol": protocol})
    return records


def merge_proxy_records(pools: list[list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], int]:
    merged: dict[str, dict[str, Any]] = {}
    duplicates = 0
    for pool in pools:
        for node in pool:
            key = str(node.get("endpoint_key") or "")
            if not key:
                continue
            if key in merged:
                duplicates += 1
                existing = merged[key]
                existing["sources"] = list(dict.fromkeys([*existing.get("sources", []), *node.get("sources", [])]))
                if not existing.get("owner") and node.get("owner"):
                    existing["owner"] = node.get("owner")
                    existing["isp"] = node.get("isp")
                if existing.get("ip_type") == "unknown" and node.get("ip_type") != "unknown":
                    for field in ("ip_type", "ip_type_confidence", "ip_type_reason"):
                        existing[field] = node.get(field)
                continue
            merged[key] = node
    return list(merged.values()), duplicates


def fetch_all_sources() -> tuple[list[dict[str, Any]], dict[str, int], int, list[str]]:
    pools: list[list[dict[str, Any]]] = []
    counts: dict[str, int] = {}
    errors: list[str] = []
    for source, url in SOURCE_URLS.items():
        try:
            records = fetch_source_records(source, url)[:MAX_PER_SOURCE]
            normalized = [node for item in records if (node := normalize_record(item, source)) is not None]
            pools.append(normalized)
            counts[source] = len(normalized)
        except Exception as exc:
            counts[source] = 0
            errors.append(f"{source}: {exc}")
    merged, duplicates = merge_proxy_records(pools)
    return merged, counts, duplicates, errors


def _recv_until(sock: socket.socket, marker: bytes, limit: int = 65536) -> bytes:
    data = b""
    while marker not in data and len(data) < limit:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("proxy response ended early")
        data += chunk
    return data


def open_proxy_tunnel(node: dict[str, Any], target_host: str, target_port: int, timeout: float = PROBE_TIMEOUT_SECONDS) -> socket.socket:
    sock = socket.create_connection((str(node["remote_host"]), int(node["remote_port"])), timeout=timeout)
    sock.settimeout(timeout)
    protocol = normalize_protocol(node.get("protocol") or node.get("proto"))
    try:
        if protocol == "http":
            authority = f"{target_host}:{target_port}"
            request = f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\nProxy-Connection: keep-alive\r\n\r\n"
            sock.sendall(request.encode("ascii"))
            response = _recv_until(sock, b"\r\n\r\n", 16384)
            first_line = response.split(b"\r\n", 1)[0]
            if b" 200 " not in first_line:
                raise ConnectionError(f"HTTP proxy CONNECT failed: {first_line.decode('ascii', errors='replace')}")
            return sock
        if protocol == "socks5":
            sock.sendall(b"\x05\x01\x00")
            if _recv_exact(sock, 2) != b"\x05\x00":
                raise ConnectionError("SOCKS5 proxy rejected no-auth method")
            try:
                packed = socket.inet_pton(socket.AF_INET, target_host)
                address = b"\x01" + packed
            except OSError:
                encoded = target_host.encode("idna")
                if len(encoded) > 255:
                    raise ValueError("target hostname is too long")
                address = b"\x03" + bytes([len(encoded)]) + encoded
            sock.sendall(b"\x05\x01\x00" + address + int(target_port).to_bytes(2, "big"))
            header = _recv_exact(sock, 4)
            if len(header) != 4 or header[1] != 0:
                raise ConnectionError(f"SOCKS5 connect failed: {header.hex()}")
            atyp = header[3]
            if atyp == 1:
                remaining = 4
            elif atyp == 4:
                remaining = 16
            elif atyp == 3:
                remaining = _recv_exact(sock, 1)[0]
            else:
                raise ConnectionError("SOCKS5 returned invalid address type")
            _recv_exact(sock, remaining)
            _recv_exact(sock, 2)
            return sock
        raise ValueError(f"unsupported public proxy protocol: {protocol}")
    except Exception:
        sock.close()
        raise


def probe_proxy(node: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    sock: socket.socket | None = None
    tls_sock: ssl.SSLSocket | None = None
    try:
        sock = open_proxy_tunnel(node, "api.ipify.org", 443)
        context = ssl.create_default_context()
        tls_sock = context.wrap_socket(sock, server_hostname="api.ipify.org")
        tls_sock.sendall(b"GET / HTTP/1.1\r\nHost: api.ipify.org\r\nConnection: close\r\nUser-Agent: AimiliVPN-Probe/1.0\r\n\r\n")
        response = b""
        while len(response) < 65536:
            chunk = tls_sock.recv(4096)
            if not chunk:
                break
            response += chunk
        if b"\r\n\r\n" not in response or b" 200 " not in response.split(b"\r\n", 1)[0]:
            raise ConnectionError("exit IP verification returned an invalid response")
        body = response.split(b"\r\n\r\n", 1)[1].strip().splitlines()[0].decode("ascii", errors="strict")
        exit_ip = str(ipaddress.ip_address(body))
        latency_ms = max(1, int((time.monotonic() - started) * 1000))
        return {
            "id": node.get("id"),
            "ip": exit_ip,
            "exit_ip": exit_ip,
            "latency_ms": latency_ms,
            "handshake_ms": latency_ms,
            "probe_status": "available",
            "probe_message": f"代理隧道及 TLS 出口验证成功: {exit_ip}",
            "probed_at": time.time(),
        }
    except Exception as exc:
        return {
            "id": node.get("id"),
            "latency_ms": 0,
            "handshake_ms": 0,
            "probe_status": "unavailable",
            "probe_message": str(exc),
            "probed_at": time.time(),
        }
    finally:
        try:
            if tls_sock is not None:
                tls_sock.close()
            elif sock is not None:
                sock.close()
        except OSError:
            pass
