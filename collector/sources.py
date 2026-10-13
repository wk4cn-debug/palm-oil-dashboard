# -*- coding: utf-8 -*-
"""
棕榈油看板 · 数据源适配器

职责：请求公开数据源 -> 解析 -> 返回结构化数据。
约束：
- 不生成、不推算、不填充任何模拟数据
- 请求或解析失败一律抛出 SourceError，由 collect.py 决定降级与标记
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Any

import requests


class SourceError(Exception):
    """数据源请求或解析失败。"""


@dataclass
class DailyBar:
    date: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    open_interest: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 新浪财经 · 期货日K接口
# ---------------------------------------------------------------------------

SINA_DAILY_HOSTS = [
    "https://stock2.finance.sina.com.cn",
    "https://stock.finance.sina.com.cn",
]

SINA_DAILY_PATH = "/futures/api/jsonp.php/var%20_{symbol}=/InnerFuturesNewService.getDailyKLine"

_BAR_RE = re.compile(
    r'\{d:"(?P<d>[^"]*)"\s*,\s*o:"(?P<o>[^"]*)"\s*,\s*h:"(?P<h>[^"]*)"\s*,\s*'
    r'l:"(?P<l>[^"]*)"\s*,\s*c:"(?P<c>[^"]*)"\s*,\s*v:"(?P<v>[^"]*)"\s*,\s*'
    r'p:"(?P<p>[^"]*)"'
)


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if value in ("", "-", "null", "None"):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_sina_daily(text: str) -> list[DailyBar]:
    bars: list[DailyBar] = []
    for m in _BAR_RE.finditer(text):
        bars.append(
            DailyBar(
                date=m.group("d"),
                open=_to_float(m.group("o")),
                high=_to_float(m.group("h")),
                low=_to_float(m.group("l")),
                close=_to_float(m.group("c")),
                volume=_to_float(m.group("v")),
                open_interest=_to_float(m.group("p")),
            )
        )
    bars.sort(key=lambda b: b.date)
    return bars


def fetch_sina_daily(
    symbol: str,
    timeout: int = 25,
    user_agent: str = "Mozilla/5.0",
) -> list[DailyBar]:
    """获取新浪财经期货日K线，按日期升序返回。

    symbol 形如 "P0"（主力连续）或 "P2701"（具体合约）。
    """
    headers = {
        "User-Agent": user_agent,
        "Referer": "https://finance.sina.com.cn/",
    }
    params = {"symbol": symbol}
    last_err: Exception | None = None

    for host in SINA_DAILY_HOSTS:
        url = host + SINA_DAILY_PATH.format(symbol=symbol)
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            resp.raise_for_status()
            text = resp.text
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue

        bars = _parse_sina_daily(text)
        if bars:
            return bars

    raise SourceError(f"新浪日K获取失败 symbol={symbol}: {last_err}")


# ---------------------------------------------------------------------------
# 新浪财经 · 实时 hq 接口（可选，仅用于诊断补充）
# ---------------------------------------------------------------------------

SINA_HQ_URL = "https://hq.sinajs.cn/list={codes}"


def fetch_sina_hq_raw(
    codes: list[str],
    timeout: int = 20,
    user_agent: str = "Mozilla/5.0",
) -> dict[str, str]:
    """返回 {code: 原始字符串}，只做原始读取，不做字段猜测解析。"""
    if not codes:
        return {}
    url = SINA_HQ_URL.format(codes=",".join(codes))
    headers = {
        "User-Agent": user_agent,
        "Referer": "https://finance.sina.com.cn/",
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = "gbk"

    out: dict[str, str] = {}
    for line in resp.text.splitlines():
        m = re.match(r'var hq_str_([A-Za-z0-9_]+)="(.*)";?', line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


# ---------------------------------------------------------------------------
# BMD FCPO（待验证，默认不启用）
# ---------------------------------------------------------------------------

def fetch_bmd_fcpo(*_args, **_kwargs) -> None:
    raise SourceError(
        "BMD FCPO 免费稳定数据源尚未验证，暂不启用。"
        "请在人工确认官方或第三方源的合规性与稳定性后，再实现对应适配器。"
    )
