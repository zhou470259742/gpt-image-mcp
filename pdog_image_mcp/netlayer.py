"""Network environment fallbacks, extracted verbatim in behavior from the skill's
generate_image.py (v2.1):

1) Fake-IP DNS fallback: Clash/mihomo Fake-IP mode makes system DNS return
   reserved-range fake addresses (198.18.0.0/15), so direct connections can
   never be established. When detected, resolve for real over DoH
   (Ali 223.5.5.5 -> Tencent 120.53.53.53 -> Cloudflare 1.1.1.1) and dial the
   real IP (Host header unchanged).
2) SNI-filtered link fallback: some international direct links reset any TLS
   ClientHello carrying SNI. For whyshy.cn hosts we keep sending no SNI, but
   after the handshake the certificate is strictly verified (trusted CA chain
   + SAN covering the target host). Verification failure aborts immediately so
   no man-in-the-middle can capture the API key.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import ssl
import tempfile
from typing import Optional
from urllib.request import Request, urlopen

import http.client

from .errors import PdogError, log

_NO_SNI_HOST_SUFFIXES = ("whyshy.cn",)
_BOGUS_IP_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in ("198.18.0.0/15", "100.64.0.0/10", "127.0.0.0/8", "0.0.0.0/8")
)
_DOH_URLS = (
    "https://223.5.5.5/resolve?name={host}&type=A",
    "https://120.53.53.53/resolve?name={host}&type=A",
    "https://1.1.1.1/resolve?name={host}&type=A",
)
_REAL_IP_CACHE: dict[str, str] = {}


def _is_ip_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _is_bogus_ip(ip: str) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return any(address in network for network in _BOGUS_IP_NETWORKS)


def _doh_resolve_a(host: str) -> Optional[str]:
    """Query A records via IP-literal DoH endpoints (bypasses system DNS)."""
    for url_template in _DOH_URLS:
        try:
            request = Request(
                url_template.format(host=host),
                headers={"Accept": "application/dns-json", "User-Agent": "Pdog-Image-Client/1.2"},
            )
            with urlopen(request, timeout=8) as response:
                data = json.loads(response.read().decode("utf-8"))
            for answer in data.get("Answer") or []:
                if answer.get("type") == 1 and answer.get("data"):
                    return answer["data"]
        except Exception:
            continue
    return None


def _resolve_real_host(host: str) -> Optional[str]:
    """Return the DoH real IP when system DNS lands in reserved/Fake-IP ranges or fails."""
    if _is_ip_address(host):
        return None
    if host in _REAL_IP_CACHE:
        return _REAL_IP_CACHE[host]
    try:
        infos = socket.getaddrinfo(host, None)
        primary = infos[0][4][0] if infos else ""
    except OSError:
        primary = ""
    if primary and not _is_bogus_ip(primary):
        return None
    real_ip = _doh_resolve_a(host)
    if real_ip and not _is_bogus_ip(real_ip):
        log(f"{host} 的 DNS 解析异常（{primary or '无法解析'}），已改用 DoH 真实 IP: {real_ip}")
        _REAL_IP_CACHE[host] = real_ip
        return real_ip
    return None


def _verify_tls_host(sock: ssl.SSLSocket, host: str) -> None:
    """A no-SNI connection carries no hostname, so the certificate must be
    checked manually to prove it really belongs to the target host. Failure
    aborts instead of continuing with a possibly intercepted channel."""
    try:
        der = sock.getpeercert(binary_form=True)
        if not der:
            raise ssl.SSLError("对端未提供证书")
        pem = ssl.DER_cert_to_PEM_cert(der)
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as handle:
            handle.write(pem)
            pem_path = handle.name
        try:
            info = ssl._ssl._test_decode_cert(pem_path)
        finally:
            os.unlink(pem_path)
        sans = [value for kind, value in info.get("subjectAltName", ()) if kind == "DNS"]
        if host not in sans:
            raise ssl.SSLError(f"证书 SAN {sans or '(无)'} 未覆盖 {host}")
        log(f"{host} 无 SNI 连接完成，证书 CA 链与 SAN 校验通过")
    except ssl.SSLError as exc:
        raise PdogError(
            f"拒绝在无 SNI 连接上继续：{host} 证书校验失败（{exc}）。"
            "当前链路可能存在中间人或网关证书异常，请检查网络环境后重试。",
            kind="network",
        ) from exc
    except (AttributeError, OSError) as exc:
        raise PdogError(
            f"拒绝在无 SNI 连接上继续：无法完成证书校验（{exc}）。",
            kind="network",
        ) from exc


class _RobustHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        if self._tunnel_host:
            # Through a proxy tunnel: TCP dials the proxy itself, then CONNECT.
            self.sock = socket.create_connection((self.host, self.port), self.timeout, self.source_address)
            self._tunnel()
        else:
            real_ip = _resolve_real_host(self.host)
            self.sock = socket.create_connection(
                (real_ip or self.host, self.port), self.timeout, self.source_address
            )
        tunnel_or_host = self._tunnel_host or self.host
        server_hostname = (
            None
            if any(suffix in tunnel_or_host for suffix in _NO_SNI_HOST_SUFFIXES)
            else tunnel_or_host
        )
        if server_hostname is None and not _is_ip_address(tunnel_or_host):
            # no-SNI channel: force CA-chain verification; host checked by _verify_tls_host
            verified_context = ssl.create_default_context()
            verified_context.check_hostname = False
            verified_context.verify_mode = ssl.CERT_REQUIRED
            self.sock = verified_context.wrap_socket(self.sock)
            _verify_tls_host(self.sock, tunnel_or_host)
        else:
            self._context.check_hostname = False
            self._context.verify_mode = ssl.CERT_NONE
            self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)


def install_opener() -> None:
    """Install the global urllib opener used by all engine HTTP calls.

    - ProxyHandler({}): bypass system proxies so the DoH/no-SNI logic controls
      the connection path directly.
    - The unverified default HTTPS context is required for IP-literal DoH
      endpoints (hostnames cannot match IP SANs); the API connection itself is
      still verified via the no-SNI strict path above.
    """
    import urllib.request

    try:
        ssl._create_default_https_context = ssl._create_unverified_context
    except AttributeError:
        pass

    class _Handler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(_RobustHTTPSConnection, req)

    try:
        urllib.request.install_opener(
            urllib.request.build_opener(urllib.request.ProxyHandler({}), _Handler)
        )
    except Exception:
        pass
