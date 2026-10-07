from __future__ import annotations

import ipaddress
import re
import socket
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any


MAX_INDEX_BYTES = 4 * 1024 * 1024
MAX_PROFILE_BYTES = 128 * 1024
USER_AGENT = "AimiliVPN-MultiSource/1.0"


COUNTRY_CODES = {
    "argentina": "AR", "australia": "AU", "brazil": "BR", "canada": "CA",
    "france": "FR", "germany": "DE", "hong kong": "HK", "india": "IN",
    "indonesia": "ID", "japan": "JP", "malaysia": "MY", "netherlands": "NL",
    "philippines": "PH", "romania": "RO", "russian federation": "RU",
    "russia": "RU", "singapore": "SG", "south korea": "KR", "korea republic of": "KR",
    "taiwan": "TW", "thailand": "TH", "turkey": "TR", "united kingdom": "GB",
    "uk": "GB", "usa": "US", "united states": "US", "viet nam": "VN", "vietnam": "VN",
}


def country_code(country: str) -> str:
    return COUNTRY_CODES.get(str(country or "").strip().lower(), "")


def _public_https_url(url: str, allowed_hosts: set[str]) -> str:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or host not in allowed_hosts:
        raise ValueError("external profile URL is not an allowed HTTPS endpoint")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("external profile URL contains unsupported authority data")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, 0, socket.SOCK_STREAM)}
    except OSError as exc:
        raise ValueError(f"external source DNS failed: {exc}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("external source resolved to a non-public address")
    return urllib.parse.urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: set[str]):
        self.allowed_hosts = allowed_hosts
        super().__init__()

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_url = _public_https_url(newurl, self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, safe_url)


def fetch_https_text(url: str, allowed_hosts: set[str], max_bytes: int, timeout: int = 15) -> str:
    safe_url = _public_https_url(url, allowed_hosts)
    opener = urllib.request.build_opener(_SafeRedirectHandler(allowed_hosts))
    request = urllib.request.Request(safe_url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain,application/x-openvpn-profile"})
    with opener.open(request, timeout=timeout) as response:
        _public_https_url(response.geturl(), allowed_hosts)
        raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("external source response is too large")
    return raw.decode("utf-8", errors="strict")


class IPSpeedTableParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.rows: list[dict[str, Any]] = []
        self._in_row = False
        self._cell_depth = 0
        self._cell_text: list[str] = []
        self._cells: list[str] = []
        self._profile_url = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "tr":
            self._in_row = True
            self._cells = []
            self._profile_url = ""
        elif self._in_row and tag in {"td", "th"}:
            self._cell_depth += 1
            self._cell_text = []
        elif self._in_row and tag == "a":
            href = dict(attrs).get("href") or ""
            if urllib.parse.urlsplit(href).path.lower().endswith(".ovpn"):
                self._profile_url = urllib.parse.urljoin(self.base_url, href)

    def handle_data(self, data: str) -> None:
        if self._in_row and self._cell_depth:
            self._cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._in_row and tag in {"td", "th"} and self._cell_depth:
            self._cells.append(" ".join("".join(self._cell_text).split()))
            self._cell_depth -= 1
            self._cell_text = []
        elif tag == "tr" and self._in_row:
            if self._profile_url:
                country = self._cells[1] if len(self._cells) > 1 else ""
                ping_text = self._cells[-1] if self._cells else ""
                match = re.search(r"\d+", ping_text)
                self.rows.append({
                    "source": "ipspeed",
                    "country": country,
                    "country_short": country_code(country),
                    "ping": int(match.group(0)) if match else 0,
                    "profile_url": self._profile_url,
                })
            self._in_row = False
            self._cell_depth = 0


def parse_ipspeed_index(html: str, base_url: str) -> list[dict[str, Any]]:
    parser = IPSpeedTableParser(base_url)
    parser.feed(html)
    return parser.rows


SCRAPER_ROW_RE = re.compile(
    r"^\s*\|\s*([^|]+?)\s*\|\s*((?:\d{1,3}\.){3}\d{1,3})\s*\|\s*([^|]*)\|\s*([^|]*)\|\s*([^|]+?)\s*\|\s*\[[^\]]*\]\(([^)]+\.ovpn)\)",
    re.IGNORECASE | re.MULTILINE,
)


def parse_vpngate_scraper_readme(markdown: str, raw_base_url: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for match in SCRAPER_ROW_RE.finditer(markdown):
        hostname, ip, ping_text, speed_text, country, profile_ref = match.groups()
        ping_match = re.search(r"\d+", ping_text)
        speed_match = re.search(r"[\d.]+", speed_text)
        rows.append({
            "source": "vpngate_scraper",
            "hostname": hostname.strip(),
            "ip": ip.strip(),
            "country": country.strip(),
            "country_short": country_code(country),
            "ping": int(ping_match.group(0)) if ping_match else 0,
            "speed_mbps": float(speed_match.group(0)) if speed_match else 0.0,
            "profile_url": urllib.parse.urljoin(raw_base_url, profile_ref.lstrip("./")),
        })
    return rows
