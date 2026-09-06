# -*- coding: utf-8 -*-
"""局域网地址探测与网络访问状态（层级 1，标准库）。"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any


def _is_usable_lan_ipv4(address: str) -> bool:
    """Return whether an address can be presented as a LAN entry point."""
    try:
        parsed = ipaddress.IPv4Address(address)
    except ipaddress.AddressValueError:
        return False
    # Do not offer loopback, APIPA, benchmarking, multicast, or unspecified
    # adapter addresses as phone-access URLs.
    return (
        parsed.is_private
        and not parsed.is_loopback
        and not parsed.is_link_local
        and parsed not in ipaddress.IPv4Network("198.18.0.0/15")
        and not parsed.is_multicast
        and not parsed.is_unspecified
    )


def get_lan_ip() -> str | None:
    """Find the LAN address selected by the current default IPv4 route.

    Connecting a UDP socket does not send traffic, but lets the OS choose the
    outbound interface. This makes the default-route address win over VPNs and
    virtual adapters. Hostname addresses are only a fallback for offline LANs.
    """
    candidates: list[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        candidates.append(sock.getsockname()[0])
    except OSError:
        pass
    finally:
        sock.close()
    try:
        candidates.extend(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return next((address for address in dict.fromkeys(candidates) if _is_usable_lan_ipv4(address)), None)


def network_access_status(host: str, port: int) -> dict[str, Any]:
    """Describe the active listener and the only LAN URL safe to present."""
    normalized_host = str(host or "").strip()
    local_url = f"http://127.0.0.1:{port}"
    try:
        bound_ip = ipaddress.ip_address(normalized_host)
    except ValueError:
        bound_ip = None

    lan_enabled = normalized_host in {"0.0.0.0", "::"}
    if isinstance(bound_ip, ipaddress.IPv4Address) and normalized_host != "0.0.0.0":
        lan_enabled = _is_usable_lan_ipv4(str(bound_ip))

    if not lan_enabled:
        return {
            "lan_enabled": False,
            "lan_url": "",
            "lan_reason": "当前服务仅允许本机访问。启用手机访问后重启即可使用。",
            "local_url": local_url,
        }

    lan_ip = str(bound_ip) if isinstance(bound_ip, ipaddress.IPv4Address) and _is_usable_lan_ipv4(str(bound_ip)) else get_lan_ip()
    if not lan_ip:
        return {
            "lan_enabled": False,
            "lan_url": "",
            "lan_reason": "未检测到可用于局域网访问的 IPv4 地址。请连接网络后重试。",
            "local_url": local_url,
        }
    return {
        "lan_enabled": True,
        "lan_url": f"http://{lan_ip}:{port}",
        "lan_reason": "",
        "local_url": local_url,
    }


