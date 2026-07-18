"""Web 请求的应用层 URL 安全检查。"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit, urlunsplit

Resolver = Callable[[str, int], Sequence[str]]


class URLPolicyError(ValueError):
    """URL 不满足公网 HTTP(S) 策略。"""


def system_resolver(host: str, port: int) -> list[str]:
    """解析目标全部地址，供请求前检查。"""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(item[4][0]) for item in infos))


def validate_public_url(url: str, resolver: Resolver = system_resolver) -> str:
    """规范化 URL，并拒绝凭据、非 HTTP scheme 与非公网目标。"""
    try:
        parsed = urlsplit(url.strip())
        port = parsed.port
    except ValueError as exc:
        raise URLPolicyError(f"URL 无效：{exc}") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise URLPolicyError("仅允许 http/https URL")
    if parsed.username is not None or parsed.password is not None:
        raise URLPolicyError("URL 不允许包含用户名或密码")
    host = parsed.hostname
    if not host:
        raise URLPolicyError("URL 缺少主机名")
    if host.rstrip(".").lower() == "localhost":
        raise URLPolicyError("不允许访问 localhost")

    target_port = port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        addresses = [host] if _is_ip_literal(host) else list(resolver(host, target_port))
    except (OSError, UnicodeError) as exc:
        raise URLPolicyError(f"DNS 解析失败：{host}") from exc
    if not addresses:
        raise URLPolicyError(f"DNS 未返回地址：{host}")
    for value in addresses:
        try:
            address = ipaddress.ip_address(value.split("%", 1)[0])
        except ValueError as exc:
            raise URLPolicyError(f"DNS 返回非法地址：{value}") from exc
        if not address.is_global:
            raise URLPolicyError(f"不允许访问非公网地址：{address}")

    scheme = parsed.scheme.lower()
    normalized_host = host.encode("idna").decode("ascii").lower()
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"
    netloc = normalized_host if port is None else f"{normalized_host}:{port}"
    return urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        return True
    except ValueError:
        return False
