"""出站请求边界: 只允许"取回内容", 不允许把任何内容发出去.

用户要求(2026-09-25): 涉及网络请求的只允许获取内容, 禁止外发内容。

做法: 用 httpx 的 MockTransport 接管真实网络, 让**真实的下载与取名实现**各跑一遍, 断言
每个出站请求都是 **GET/HEAD、没有请求体、没有凭据头**, 且目标主机在允许清单里(公开的
Steam CDN 与公开商店接口)。再加两条静态扫描兜底: 整棵树里不能出现写请求、也不能出现
允许清单之外的地址或第二个 HTTP 客户端。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from archive_management.services.artwork import STEAM_CDN_ROOT, HttpArtworkFetcher
from archive_management.services.game_names import HttpNameFetcher
from reporting import SecurityRecorder

pytestmark = [
    pytest.mark.security,
    pytest.mark.critical,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("不可信输入防护"),
    pytest.mark.story("网络只下行不外发"),
    pytest.mark.layer("security"),
    pytest.mark.timeout(120),
]

_CATEGORY = "outbound_requests"
# 允许访问的主机: 只有公开的 Steam 图片 CDN 与公开商店接口(无凭据方案)。
_ALLOWED_HOSTS = frozenset({"cdn.cloudflare.steamstatic.com", "store.steampowered.com"})
# 只允许"取回内容"的 HTTP 方法。
_ALLOWED_METHODS = frozenset({"GET", "HEAD"})
# 凭据类请求头: 出现即视为把身份信息发了出去(本项目不引入任何平台凭据)。
_CREDENTIAL_HEADERS = ("authorization", "cookie", "proxy-authorization")
# 合法的最小 PNG 头: 让真实的下载路径能走到"拿到字节"这一步。
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
# 源码里"写请求"与"别的 HTTP 客户端"的入口。
# 只点名 HTTP 客户端上的写方法: 仓库里 `service.delete(...)` / `cache.put(...)` 这类同名
# 方法很多, 用裸 `.delete(` 匹配会变成"什么都拦"。
_BANNED_OUTBOUND_TOKENS = (
    "httpx.post(",
    "httpx.put(",
    "httpx.patch(",
    "httpx.delete(",
    "client.post(",
    "client.put(",
    "client.patch(",
    "client.delete(",
    "urlopen(",
    "import requests",
    "from requests",
    "urllib.request",
)


def _client(handler: object) -> httpx.Client:
    """构造一个只走假传输的客户端(真实网络一次都不碰)."""
    return httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def _outbound_violations(request: httpx.Request) -> list[str]:
    """返回这个请求违反"只下行"的地方(空列表 = 合规)."""
    violations: list[str] = []
    if request.method not in _ALLOWED_METHODS:
        violations.append(f"方法不是只读: {request.method}")
    if request.content:
        violations.append(f"请求带了内容: {len(request.content)} 字节")
    if request.url.host not in _ALLOWED_HOSTS:
        violations.append(f"目标主机不在允许清单: {request.url.host}")
    lower_headers = {name.lower() for name in request.headers}
    violations.extend(
        f"发送了凭据类请求头: {header}"
        for header in _CREDENTIAL_HEADERS
        if header in lower_headers
    )
    return violations


def _assert_download_only(
    requests: list[httpx.Request],
    recorder: SecurityRecorder,
    *,
    scenario: str,
) -> None:
    """断言这一批请求全是"只下行", 并把结论写进安全报告."""
    violations = [
        f"{request.method} {request.url}: {item}"
        for request in requests
        for item in _outbound_violations(request)
    ]
    recorder.expect_blocked(
        category=_CATEGORY,
        scenario=scenario,
        input_summary=", ".join(f"{item.method} {item.url}" for item in requests),
        expected="只允许 GET/HEAD、无请求体、无凭据头、主机在允许清单内",
        actual="全部合规" if not violations else "; ".join(violations),
        blocked=bool(requests) and not violations,
    )
    assert requests, "假传输没有收到任何请求: 这条用例失去了意义"
    assert violations == [], f"出站请求不合规: {violations}"


def test_artwork_download_only_fetches_content(
    security_recorder: SecurityRecorder,
) -> None:
    """封面下载: GET、无请求体、不带凭据头, 且只访问公开图片 CDN."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, content=_PNG_BYTES, headers={"content-type": "image/png"}
        )

    fetcher = HttpArtworkFetcher(client=_client(handler))
    fetched = fetcher.fetch(
        f"{STEAM_CDN_ROOT}/12345/header.jpg", timeout=5.0, max_bytes=1024 * 1024
    )

    assert fetched.content == _PNG_BYTES
    _assert_download_only(seen, security_recorder, scenario="封面下载")


def test_name_lookup_only_fetches_content(security_recorder: SecurityRecorder) -> None:
    """译名查询: 只把 AppID 与语言放进查询串, 不带请求体."""
    seen: list[httpx.Request] = []
    payload = {"12345": {"success": True, "data": {"name": "Outer Wilds"}}}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            content=json.dumps(payload).encode("utf-8"),
            headers={"content-type": "application/json"},
        )

    fetcher = HttpNameFetcher(client=_client(handler))
    name = fetcher.fetch("12345", language="schinese", timeout=5.0, max_bytes=64 * 1024)

    assert name == "Outer Wilds"
    assert seen[0].url.host == "store.steampowered.com"
    _assert_download_only(seen, security_recorder, scenario="译名查询")


def test_source_tree_has_no_outbound_write_calls() -> None:
    """静态兜底: 源码里不出现写请求, 也不引入第二个 HTTP 客户端."""
    source_root = Path(__file__).resolve().parents[2] / "src"
    offenders = [
        f"{module.relative_to(source_root)}: {token}"
        for module in sorted(source_root.rglob("*.py"))
        for token in _BANNED_OUTBOUND_TOKENS
        if token in module.read_text(encoding="utf-8")
    ]

    assert offenders == [], f"源码里出现外发内容/额外客户端的入口: {offenders}"


def test_every_url_in_the_source_tree_is_on_the_allowlist() -> None:
    """静态兜底: 源码里出现的每个地址都必须在允许清单内(防止悄悄加一个新主机)."""
    source_root = Path(__file__).resolve().parents[2] / "src"
    hosts: dict[str, set[str]] = {}
    for module in sorted(source_root.rglob("*.py")):
        text = module.read_text(encoding="utf-8")
        for host in re.findall(r"https?://([A-Za-z0-9.\-]+)", text):
            hosts.setdefault(host, set()).add(str(module.relative_to(source_root)))

    unknown = {
        host: sorted(files)
        for host, files in hosts.items()
        if host not in _ALLOWED_HOSTS
    }
    assert unknown == {}, f"出现允许清单之外的地址: {unknown}"
    assert hosts, "源码里没有任何地址: 这条用例失去了意义"
