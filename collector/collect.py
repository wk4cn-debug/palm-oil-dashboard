# -*- coding: utf-8 -*-
"""
棕榈油看板 · 采集主脚本

输出：
- data/latest.json       最新看板数据（含近 N 个交易日历史，供页面画图）
- data/latest.js         window.__PALM_DATA__ = {...}，用于本地 HTML 绕过 CORS
- data/history/YYYY-MM-DD.json   每个交易日一条有效行情
- data/revisions.json    数据修正记录

原则：
- 不编造、不估算、不插值
- 采集失败保留上次有效数据，但标记为数据过期/采集异常
- 同一交易日多次采集只算一个交易日
"""

from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sources import SourceError, fetch_sina_daily  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "collector" / "config.yml"
DATA_DIR = ROOT / "data"
HISTORY_DIR = DATA_DIR / "history"
LATEST_JSON = DATA_DIR / "latest.json"
LATEST_JS = DATA_DIR / "latest.js"
REVISIONS_JSON = DATA_DIR / "revisions.json"

CN_TZ = timezone(timedelta(hours=8))
DISCLAIMER = "该看板数据由wzy收集整理，仅供辅助参考"

FUNDAMENTAL_SLOTS = [
    ("china_palm_port_inventory", "中国棕榈油港口库存"),
    ("mpob", "MPOB 马来西亚棕榈油局月度报告"),
    ("indonesia_supply_demand", "印尼棕榈油供需"),
    ("biodiesel_policy", "生物柴油政策"),
    ("enso", "ENSO 天气（厄尔尼诺 / 拉尼娜）"),
    ("other_oils", "其他油脂供需"),
]


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def read_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return default
    return default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_future_contract_code(prefix: str, month: str, today: datetime, plus_years: int = 0) -> str:
    year = today.year
    if int(month) < today.month:
        year += 1
    year += plus_years
    return f"{prefix}{year % 100:02d}{month}"


# ---------------------------------------------------------------------------
# 采集逻辑
# ---------------------------------------------------------------------------

def collect_main_contract(symbol_cfg: dict, settings: dict, today: datetime):
    """采集某一品种的主力连续行情与历史。"""
    code = symbol_cfg["sina_main"]
    bars = fetch_sina_daily(
        code,
        timeout=settings.get("request_timeout", 25),
        user_agent=settings.get("user_agent", "Mozilla/5.0"),
    )

    history_days = int(settings.get("history_days", 30))
    recent = bars[-history_days:] if len(bars) > history_days else bars

    last = recent[-1]
    prev = recent[-2] if len(recent) >= 2 else None

    change = None
    change_pct = None
    if prev and last.close is not None and prev.close not in (None, 0):
        change = round(last.close - prev.close, 4)
        change_pct = round(change / prev.close * 100, 4)

    trade_date = last.date
    is_today = trade_date == today.strftime("%Y-%m-%d")
    status = "收盘" if is_today else f"收盘（最近交易日 {trade_date}）"

    return {
        "contract": code,
        "contract_status": "主力合约待确认（使用主力连续口径，换月可能产生跳空）",
        "price": last.close,
        "change": change,
        "change_pct": change_pct,
        "quote_time": trade_date,
        "trade_date": trade_date,
        "status": status,
        "open": last.open,
        "high": last.high,
        "low": last.low,
        "volume": last.volume,
        "open_interest": last.open_interest,
        "history": [
            {"date": b.date, "close": b.close, "open": b.open, "high": b.high, "low": b.low}
            for b in recent
        ],
        "_bars": recent,
    }


def collect_future_months(symbol_cfg: dict, settings: dict, today: datetime) -> list[dict]:
    """采集关键未来月份合约，只返回实际拿到报价的月份，其余标暂不可用。"""
    prefix = symbol_cfg.get("sina_prefix")
    months = symbol_cfg.get("future_months", [])
    results: list[dict] = []

    for month in months:
        entry = {
            "month": month,
            "contract": None,
            "price": None,
            "change": None,
            "change_pct": None,
            "quote_time": None,
            "source": None,
            "status": "暂不可用",
        }

        if not prefix:
            entry["status"] = "暂不可用（未配置合约前缀）"
            results.append(entry)
            continue

        got = False
        for extra_years in (0, 1):
            contract = build_future_contract_code(prefix, month, today, extra_years)
            try:
                bars = fetch_sina_daily(
                    contract,
                    timeout=settings.get("request_timeout", 25),
                    user_agent=settings.get("user_agent", "Mozilla/5.0"),
                )
            except SourceError:
                continue

            if not bars:
                continue

            last = bars[-1]
            prev = bars[-2] if len(bars) >= 2 else None
            change = None
            change_pct = None
            if prev and last.close is not None and prev.close not in (None, 0):
                change = round(last.close - prev.close, 4)
                change_pct = round(change / prev.close * 100, 4)

            entry.update(
                {
                    "contract": contract,
                    "price": last.close,
                    "change": change,
                    "change_pct": change_pct,
                    "quote_time": last.date,
                    "source": "新浪财经 · 期货日K接口",
                    "status": "收盘" if last.date == today.strftime("%Y-%m-%d")
                    else f"最近交易日 {last.date}",
                }
            )
            got = True
            break

        if not got:
            entry["status"] = "暂不可用"
        results.append(entry)

    return results


# ---------------------------------------------------------------------------
# 历史归档与修正记录
# ---------------------------------------------------------------------------

def archive_history(symbol: str, item: dict, collected_at: str, source_label: str) -> list[dict]:
    """把近 N 个交易日的收盘数据写入 history/YYYY-MM-DD.json。

    已存在且价格一致的交易日不重复写入；价格不同则记录修正。
    """
    revisions: list[dict] = []
    bars = item.get("_bars") or []

    for bar in bars:
        path = HISTORY_DIR / f"{bar.date}.json"
        old = read_json(path, None)
        record = {
            "trade_date": bar.date,
            "symbol": symbol,
            "contract": item.get("contract"),
            "contract_status": item.get("contract_status"),
            "price": bar.close,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "volume": bar.volume,
            "open_interest": bar.open_interest,
            "quote_time": bar.date,
            "collected_at": collected_at,
            "source": source_label,
            "status": "收盘",
        }

        if old is None:
            write_json(path, record)
            continue

        if old.get("price") == record["price"] and old.get("symbol") == symbol:
            # 已存在且一致，只更新采集时间之外的关键字段保持不变
            continue

        revisions.append(
            {
                "trade_date": bar.date,
                "symbol": symbol,
                "old_price": old.get("price"),
                "new_price": record["price"],
                "corrected_at": collected_at,
                "source": source_label,
                "reason": "可信来源修正",
            }
        )
        write_json(path, record)

    return revisions


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    cfg = load_config()
    settings = cfg.get("settings", {})
    symbols = cfg.get("symbols", {})
    source_registry = cfg.get("sources", {})

    collected_at = now_cn().isoformat(timespec="seconds")
    today = now_cn()
    today_str = today.strftime("%Y-%m-%d")

    previous = read_json(LATEST_JSON, {})
    previous_items = previous.get("items", {}) if isinstance(previous, dict) else {}

    items: dict[str, dict] = {}
    abnormal: list[str] = []
    used_sources: list[str] = []
    all_revisions: list[dict] = []

    for code, symbol_cfg in symbols.items():
        name = symbol_cfg.get("name", code)
        short_name = symbol_cfg.get("short_name", code)

        if not symbol_cfg.get("enabled", False):
            items[code] = {
                "name": name,
                "short_name": short_name,
                "exchange": symbol_cfg.get("exchange"),
                "contract": None,
                "contract_status": "暂不可用",
                "price": None,
                "change": None,
                "change_pct": None,
                "quote_time": None,
                "trade_date": None,
                "source": None,
                "source_note": "数据源尚未验证，未启用",
                "status": "暂不可用",
                "history": [],
                "future_contracts": [
                    {
                        "month": m,
                        "contract": None,
                        "price": None,
                        "change": None,
                        "change_pct": None,
                        "quote_time": None,
                        "source": None,
                        "status": "暂不可用",
                    }
                    for m in symbol_cfg.get("future_months", [])
                ],
            }
            abnormal.append(f"{short_name} 暂不可用（数据源未启用）")
            continue

        source_label = source_registry.get("sina_daily", {}).get(
            "label", "新浪财经 · 期货日K接口"
        )

        try:
            item = collect_main_contract(symbol_cfg, settings, today)
            item["name"] = name
            item["short_name"] = short_name
            item["exchange"] = symbol_cfg.get("exchange")
            item["source"] = source_label
            item["source_note"] = source_registry.get("sina_daily", {}).get("note", "")

            try:
                item["future_contracts"] = collect_future_months(symbol_cfg, settings, today)
            except Exception:  # noqa: BLE001
                item["future_contracts"] = [
                    {
                        "month": m,
                        "contract": None,
                        "price": None,
                        "change": None,
                        "change_pct": None,
                        "quote_time": None,
                        "source": None,
                        "status": "暂不可用",
                    }
                    for m in symbol_cfg.get("future_months", [])
                ]

            revisions = archive_history(code, item, collected_at, source_label)
            all_revisions.extend(revisions)

            item.pop("_bars", None)
            items[code] = item
            used_sources.append(source_label)

        except Exception as exc:  # noqa: BLE001
            # 采集失败：保留上次有效数据，但标记为过期/异常
            traceback.print_exc()
            prev_item = previous_items.get(code)
            if isinstance(prev_item, dict) and prev_item.get("price") is not None:
                fallback = dict(prev_item)
                fallback["status"] = "数据过期 / 采集异常（保留上次有效数据）"
                fallback["source_note"] = f"本次采集失败：{exc}"
                items[code] = fallback
                abnormal.append(f"{short_name} 采集异常，保留 {fallback.get('trade_date')} 数据")
            else:
                items[code] = {
                    "name": name,
                    "short_name": short_name,
                    "exchange": symbol_cfg.get("exchange"),
                    "contract": None,
                    "contract_status": "暂不可用",
                    "price": None,
                    "change": None,
                    "change_pct": None,
                    "quote_time": None,
                    "trade_date": None,
                    "source": None,
                    "source_note": f"首次采集失败：{exc}",
                    "status": "采集异常",
                    "history": [],
                    "future_contracts": [
                        {
                            "month": m,
                            "contract": None,
                            "price": None,
                            "change": None,
                            "change_pct": None,
                            "quote_time": None,
                            "source": None,
                            "status": "暂不可用",
                        }
                        for m in symbol_cfg.get("future_months", [])
                    ],
                }
                abnormal.append(f"{short_name} 采集异常且无历史有效数据")

    if not abnormal:
        market_status = "正常"
    elif all(v.get("price") is None for v in items.values()):
        market_status = "采集异常"
    else:
        market_status = "部分异常"

    fundamentals = {
        key: {"label": label, "status": "未接入", "note": "后续迭代内容，尚未接入"}
        for key, label in FUNDAMENTAL_SLOTS
    }

    latest = {
        "schema_version": "1.0",
        "dashboard_generated_at": now_cn().isoformat(timespec="seconds"),
        "actual_collected_at": collected_at,
        "trade_date": today_str,
        "market_status": market_status,
        "abnormal_items": abnormal,
        "items": items,
        "fundamentals": fundamentals,
        "sources": {
            k: {"label": v.get("label"), "enabled": v.get("enabled", False), "note": v.get("note", "")}
            for k, v in source_registry.items()
        },
        "source_list": sorted(set(used_sources)),
        "disclaimer": DISCLAIMER,
    }

    write_json(LATEST_JSON, latest)

    payload = json.dumps(latest, ensure_ascii=False).replace("</", "<\\/")
    LATEST_JS.write_text(
        f"window.__PALM_DATA__ = {payload};\n",
        encoding="utf-8",
    )

    existing_revisions = read_json(REVISIONS_JSON, [])
    if not isinstance(existing_revisions, list):
        existing_revisions = []
    if all_revisions:
        existing_revisions.extend(all_revisions)
        write_json(REVISIONS_JSON, existing_revisions)

    print(f"[ok] collected_at={collected_at} status={market_status} abnormal={abnormal}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(1)
