"""
scanner.py — TradingView-powered momentum scanner (SiteScout paid add-on)

Ported from Desktop/lead-finder/backend/main.py's Trading Hub routes. Pure
TradingView-scraper + math — no AI calls, no per-request cost.
"""

from __future__ import annotations

import time
import asyncio
from datetime import datetime, timezone

import httpx

_TV_SCAN_URL = "https://scanner.tradingview.com/america/scan"
_TV_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
}
_TV_COLS = [
    "name", "description", "close", "change", "change|1W", "change|1M",
    "volume", "relative_volume_10d_calc", "market_cap_basic", "sector",
    "Recommend.All", "RSI", "EMA20", "EMA50", "EMA200",
    "price_52_week_high", "price_52_week_low",
    "average_volume_10d_calc", "average_volume_90d_calc", "ATR",
    "earnings_per_share_diluted_yoy_growth_ttm", "total_revenue_yoy_growth_ttm",
    "high", "low", "Perf.3M",
]
_IPO_SHORT_COLS = _TV_COLS + ["ipo_offer_date"]

_YF_HDRS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept": "application/json,*/*;q=0.8",
    "Referer": "https://finance.yahoo.com/",
}

_TV_SECTOR_TO_ETF = {
    "Electronic Technology": "XLK", "Technology Services": "XLK",
    "Finance": "XLF", "Energy Minerals": "XLE",
    "Health Technology": "XLV", "Health Services": "XLV",
    "Producer Manufacturing": "XLI", "Industrial Services": "XLI",
    "Transportation": "XLI", "Commercial Services": "XLI",
    "Process Industries": "XLB", "Non-Energy Minerals": "XLB",
    "Communications": "XLC", "Consumer Services": "XLY",
    "Consumer Durables": "XLY", "Retail Trade": "XLY",
    "Consumer Non-Durables": "XLP", "Utilities": "XLU", "Real Estate": "XLRE",
    "Distribution Services": "XLI", "Miscellaneous": None, "Government": None,
}

_SECTOR_NAMES = {
    "XLK": "Technology", "XLF": "Financials", "XLE": "Energy", "XLV": "Health Care",
    "XLY": "Consumer Disc", "XLI": "Industrials", "XLB": "Materials", "XLC": "Comm Services",
    "XLRE": "Real Estate", "XLU": "Utilities", "XLP": "Consumer Staples",
}

_STRENGTH_COLS = [
    "name", "description", "close", "change", "change|1W", "change|1M", "change|3M",
    "volume", "relative_volume_10d_calc", "market_cap_basic", "sector",
    "Recommend.All", "RSI", "EMA20", "EMA50", "EMA200",
    "price_52_week_high", "price_52_week_low",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _tv_format(raw: dict) -> list[dict]:
    stocks = []
    for item in raw.get("data", []):
        s = dict(zip(_TV_COLS, item["d"]))
        s["ticker"] = item["s"].split(":")[-1]
        px = s.get("close") or 0
        hi = s.get("price_52_week_high") or 0
        lo = s.get("price_52_week_low") or 0
        if hi > lo > 0:
            s["pos_52w"] = round(((px - lo) / (hi - lo)) * 100, 1)
        s["above_ema20"] = px > (s.get("EMA20") or 0)
        s["above_ema50"] = px > (s.get("EMA50") or 0)
        s["above_ema200"] = px > (s.get("EMA200") or 0)
        s["ema_score"] = sum([s["above_ema20"], s["above_ema50"], s["above_ema200"]])
        rec = s.get("Recommend.All") or 0
        s["signal"] = "STRONG BUY" if rec >= 0.5 else "BUY" if rec >= 0.1 else "NEUTRAL" if rec > -0.1 else "SELL"
        atr = s.get("ATR") or 0
        s["adr_pct"] = round(atr / px * 100, 2) if px else None
        v10, v90 = s.get("average_volume_10d_calc") or 0, s.get("average_volume_90d_calc") or 0
        s["vol_dryup"] = round(v10 / v90, 2) if v90 else None
        s["rvol"] = round(s.get("relative_volume_10d_calc") or 0, 2)
        s["eps_yoy"] = round(s["earnings_per_share_diluted_yoy_growth_ttm"], 1) if s.get("earnings_per_share_diluted_yoy_growth_ttm") is not None else None
        s["rev_yoy"] = round(s["total_revenue_yoy_growth_ttm"], 1) if s.get("total_revenue_yoy_growth_ttm") is not None else None
        day_hi, day_lo = s.get("high") or 0, s.get("low") or 0
        if day_hi and day_lo:
            s["trigger"] = round(day_hi, 2)
            s["stop_sugg"] = round(day_lo, 2)
            s["risk_per_share"] = round(day_hi - day_lo, 2)
        ema20, ema50 = s.get("EMA20") or 0, s.get("EMA50") or 0
        if px and hi and ema20 and ema50:
            ext50 = (px - ema50) / ema50 * 100
            if ext50 > 20:
                s["stage"] = "Climax?"
            elif px >= hi * 0.95 and abs(px - ema20) / ema20 * 100 <= 8:
                s["stage"] = "Base n' Break"
            elif abs(px - ema20) / ema20 * 100 <= 3 and ema20 > ema50:
                s["stage"] = "EMA Crossback"
            elif px > ema50:
                s["stage"] = "Uptrend"
            else:
                s["stage"] = "Rebuilding"
        for k, v in list(s.items()):
            if isinstance(v, float):
                s[k] = round(v, 3)
        stocks.append(s)
    return stocks


def _format_ipo_shorts(raw: dict) -> list[dict]:
    stocks = []
    now_ts = time.time()
    for item in raw.get("data", []):
        s = dict(zip(_IPO_SHORT_COLS, item["d"]))
        s["ticker"] = item["s"].split(":")[-1]
        px = s.get("close") or 0
        hi = s.get("price_52_week_high") or 0
        lo = s.get("price_52_week_low") or 0
        if hi > lo > 0:
            s["pos_52w"] = round(((px - lo) / (hi - lo)) * 100, 1)
        s["above_ema20"] = px > (s.get("EMA20") or 0)
        s["above_ema50"] = px > (s.get("EMA50") or 0)
        s["above_ema200"] = px > (s.get("EMA200") or 0)
        s["dist_from_52w_high_pct"] = round((px / hi - 1) * 100, 1) if hi else None
        ipo_ts = s.get("ipo_offer_date")
        s["days_since_ipo"] = round((now_ts - ipo_ts) / 86400) if ipo_ts else None
        s["ipo_date"] = (
            datetime.fromtimestamp(ipo_ts, tz=timezone.utc).strftime("%Y-%m-%d")
            if ipo_ts else None
        )
        for k, v in list(s.items()):
            if isinstance(v, float):
                s[k] = round(v, 3)
        stocks.append(s)
    return stocks


async def _yf_daily(client: httpx.AsyncClient, ticker: str, months: int = 3) -> tuple[list, list]:
    """Daily (closes, volumes) for the last N months. Empty lists on failure."""
    try:
        r = await client.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range={months}mo",
            headers=_YF_HDRS, timeout=8,
        )
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        pairs = [(c, v) for c, v in zip(q["close"], q["volume"]) if c is not None]
        return [p[0] for p in pairs], [p[1] or 0 for p in pairs]
    except Exception:
        return [], []


def _quality_score(s: dict) -> int:
    """Conviction score 1-10 using Qullamaggie/Minervini criteria."""
    score = 0
    rs = s.get("rs_vs_spy", 0)
    rsi = s.get("RSI") or 0
    chg1w = s.get("change|1W") or 0
    rvol = s.get("relative_volume_10d_calc") or 0
    sec_rs = s.get("sector_rs", 0)

    if rs > 15:
        score += 3
    elif rs > 8:
        score += 2
    elif rs > 0:
        score += 1

    if sec_rs > 5:
        score += 2
    elif sec_rs > 2:
        score += 1

    if 50 <= rsi <= 62:
        score += 2
    elif 45 <= rsi <= 68:
        score += 1

    if -2 <= chg1w <= 3:
        score += 2
    elif -4 <= chg1w <= 5:
        score += 1

    if rvol > 1.3:
        score += 1

    return min(score, 10)


def _classify(s: dict, sector_rs_map: dict, spy_1m: float) -> dict:
    px = s.get("close") or 0
    rsi = s.get("RSI") or 0
    chg1w = s.get("change|1W") or 0
    chg1m = s.get("change|1M") or 0
    hi52 = s.get("price_52_week_high") or 0
    lo52 = s.get("price_52_week_low") or 0
    rvol = s.get("relative_volume_10d_calc") or 0

    ema20 = s.get("EMA20") or 0
    ema50 = s.get("EMA50") or 0
    ema200 = s.get("EMA200") or 0
    ema_score = sum([px > ema20 > 0, px > ema50 > 0, px > ema200 > 0])

    rs_vs_spy = round(chg1m - spy_1m, 2)
    etf = _TV_SECTOR_TO_ETF.get(s.get("sector", ""))
    sector_rs = sector_rs_map.get(etf, 0) if etf else 0
    sector_leading = sector_rs > 0

    pct_from_high = round((px / hi52 - 1) * 100, 1) if hi52 > 0 else None
    pos_52w = round(((px - lo52) / (hi52 - lo52)) * 100, 1) if hi52 > lo52 > 0 else None

    s.update({
        "ema_score": ema_score, "rs_vs_spy": rs_vs_spy,
        "sector_etf": etf or "", "sector_rs": round(sector_rs, 2),
        "sector_leading": sector_leading,
        "pct_from_52w_high": pct_from_high, "pos_52w": pos_52w,
    })

    if (ema_score == 3 and 40 <= rsi <= 70 and chg1m >= 5
            and -5 <= chg1w <= 8 and sector_leading and rs_vs_spy > 0):
        s["verdict"] = "TRADABLE"
        s["verdict_reason"] = f"Stage 2 · RS +{rs_vs_spy}% vs SPY · RSI {round(rsi)} · {etf or 'leading sector'}"
    elif (ema_score >= 2 and 35 <= rsi <= 75 and chg1m >= 5 and rs_vs_spy > -5):
        s["verdict"] = "SHARES WORTHY"
        s["verdict_reason"] = f"EMA {ema_score}/3 · RS {'+' if rs_vs_spy >= 0 else ''}{rs_vs_spy}% · RSI {round(rsi)}"
    else:
        reasons = []
        if ema_score < 2:
            reasons.append(f"weak EMAs ({ema_score}/3)")
        if not (35 <= rsi <= 75):
            reasons.append(f"RSI {round(rsi)}")
        if not sector_leading:
            reasons.append("weak sector")
        if chg1m < 5:
            reasons.append("no trend")
        s["verdict"] = "HANDS OFF"
        s["verdict_reason"] = " · ".join(reasons) or "doesn't qualify"

    s["quality_score"] = _quality_score(s)
    return s


# ---------------------------------------------------------------------------
# Public — Market Mode
# ---------------------------------------------------------------------------
async def get_market_mode() -> dict:
    payload = {
        "symbols": {"tickers": ["AMEX:SPY", "NASDAQ:QQQ"]},
        "columns": ["name", "close", "change", "change|1W", "change|1M", "RSI", "EMA20", "EMA50", "EMA200", "Recommend.All"],
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(_TV_SCAN_URL, json=payload, headers=_TV_HEADERS)
            r.raise_for_status()
            data = r.json()
        cols = ["name", "close", "change", "change|1W", "change|1M", "RSI", "EMA20", "EMA50", "EMA200", "Recommend.All"]
        items = {}
        for item in data.get("data", []):
            t = item["s"].split(":")[-1]
            d = dict(zip(cols, item["d"]))
            px = d.get("close") or 0
            d["above_ema20"] = px > (d.get("EMA20") or 0)
            d["above_ema50"] = px > (d.get("EMA50") or 0)
            d["above_ema200"] = px > (d.get("EMA200") or 0)
            d["ema_score"] = sum([d["above_ema20"], d["above_ema50"], d["above_ema200"]])
            items[t] = d
        spy = items.get("SPY", {})
        qqq = items.get("QQQ", {})
        spy_score = spy.get("ema_score", 0)
        qqq_score = qqq.get("ema_score", 0)
        avg_score = (spy_score + qqq_score) / 2
        spy_1m = spy.get("change|1M") or 0
        if avg_score >= 2.5 and spy_1m >= 0:
            mode = "green"
            label = "🟢 Full Size OK"
            desc = "Market trending — options on A+ setups, shares on the rest"
        elif avg_score >= 1.5 or spy.get("above_ema200"):
            mode = "yellow"
            label = "🟡 Shares Only"
            desc = "Market choppy — skip options, shares on best setups only"
        else:
            mode = "red"
            label = "🔴 Cash Only"
            desc = "Market in downtrend — watch only, no new positions"

        dist_days = None
        ftd = False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                closes, vols = await _yf_daily(client, "SPY", months=3)
            if len(closes) >= 26:
                c25, v25 = closes[-26:], vols[-26:]
                dist_days = sum(
                    1 for i in range(1, len(c25))
                    if (c25[i] / c25[i - 1] - 1) <= -0.002 and v25[i] > v25[i - 1]
                )
                low_idx = min(range(len(c25)), key=lambda i: c25[i])
                for i in range(low_idx + 3, len(c25)):
                    if (c25[i] / c25[i - 1] - 1) >= 0.0125 and v25[i] > v25[i - 1]:
                        ftd = True
                        break
            if dist_days is not None and dist_days >= 5 and mode == "green":
                mode, label = "yellow", "🟡 Shares Only"
                desc = f"EMAs fine but {dist_days} distribution days in 25 sessions — institutions are selling. Tighten up."
            elif dist_days is not None:
                desc += f" · {dist_days} dist day{'s' if dist_days != 1 else ''}/25"
                if ftd and mode != "green":
                    desc += " · follow-through day printed — rally attempt confirmed"
        except Exception as e:
            print(f"[market-mode] dist-day calc failed: {e}")

        return {"mode": mode, "label": label, "desc": desc, "spy": spy, "qqq": qqq,
                "distribution_days": dist_days, "follow_through": ftd}
    except Exception as e:
        return {"mode": "unknown", "label": "⚪ No Data", "desc": str(e), "spy": {}, "qqq": {}}


# ---------------------------------------------------------------------------
# Public — Scan (pre-breakout / in-trend / episodic pivots / HTF / IPO shorts / sectors)
# ---------------------------------------------------------------------------
async def get_scan() -> dict:
    pre_payload = {
        "filter": [
            {"left": "RSI", "operation": "greater", "right": 45},
            {"left": "RSI", "operation": "less", "right": 65},
            {"left": "change|1M", "operation": "greater", "right": 5},
            {"left": "change|1M", "operation": "less", "right": 30},
            {"left": "change|1W", "operation": "greater", "right": -3},
            {"left": "change|1W", "operation": "less", "right": 4},
            {"left": "Recommend.All", "operation": "greater", "right": 0.2},
            {"left": "volume", "operation": "greater", "right": 300000},
            {"left": "market_cap_basic", "operation": "greater", "right": 300000000},
            {"left": "close", "operation": "greater", "right": 10},
        ],
        "columns": _TV_COLS,
        "sort": {"sortBy": "change|1M", "sortOrder": "desc"},
        "range": [0, 150],
    }
    trend_payload = {
        "filter": [
            {"left": "RSI", "operation": "greater", "right": 50},
            {"left": "RSI", "operation": "less", "right": 75},
            {"left": "change|1M", "operation": "greater", "right": 15},
            {"left": "change|1W", "operation": "greater", "right": -8},
            {"left": "change|1W", "operation": "less", "right": 3},
            {"left": "Recommend.All", "operation": "greater", "right": 0.3},
            {"left": "volume", "operation": "greater", "right": 300000},
            {"left": "market_cap_basic", "operation": "greater", "right": 300000000},
            {"left": "close", "operation": "greater", "right": 10},
        ],
        "columns": _TV_COLS,
        "sort": {"sortBy": "change|1M", "sortOrder": "desc"},
        "range": [0, 100],
    }
    ep_payload = {
        "filter": [
            {"left": "change", "operation": "greater", "right": 8},
            {"left": "relative_volume_10d_calc", "operation": "greater", "right": 3},
            {"left": "volume", "operation": "greater", "right": 1000000},
            {"left": "market_cap_basic", "operation": "greater", "right": 300000000},
            {"left": "close", "operation": "greater", "right": 5},
        ],
        "columns": _TV_COLS,
        "sort": {"sortBy": "relative_volume_10d_calc", "sortOrder": "desc"},
        "range": [0, 100],
    }
    htf_payload = {
        "filter": [
            {"left": "Perf.3M", "operation": "greater", "right": 90},
            {"left": "volume", "operation": "greater", "right": 300000},
            {"left": "market_cap_basic", "operation": "greater", "right": 300000000},
            {"left": "close", "operation": "greater", "right": 5},
        ],
        "columns": _TV_COLS,
        "sort": {"sortBy": "Perf.3M", "sortOrder": "desc"},
        "range": [0, 150],
    }
    sector_etfs = ["XLK", "XLF", "XLE", "XLV", "XLY", "XLI", "XLB", "XLC", "XLRE", "XLU", "XLP", "SPY"]
    sector_payload = {
        "symbols": {"tickers": [f"AMEX:{e}" for e in sector_etfs]},
        "columns": ["name", "close", "change", "change|1W", "change|1M", "change|3M"],
    }
    ipo_cutoff_ts = int(time.time()) - (30 * 30 * 86400)
    ipo_short_payload = {
        "filter": [
            {"left": "ipo_offer_date", "operation": "greater", "right": ipo_cutoff_ts},
            {"left": "ipo_blank_check_flag", "operation": "equal", "right": False},
            {"left": "RSI", "operation": "less", "right": 50},
            {"left": "change|1M", "operation": "less", "right": 0},
            {"left": "volume", "operation": "greater", "right": 300000},
            {"left": "market_cap_basic", "operation": "greater", "right": 300000000},
            {"left": "market_cap_basic", "operation": "less", "right": 50000000000},
            {"left": "close", "operation": "greater", "right": 5},
        ],
        "columns": _IPO_SHORT_COLS,
        "sort": {"sortBy": "change|1M", "sortOrder": "asc"},
        "range": [0, 100],
    }

    async with httpx.AsyncClient(timeout=15) as client:
        r_pre, r_trend, r_ep, r_htf, r_sectors, r_ipo = await asyncio.gather(
            client.post(_TV_SCAN_URL, json=pre_payload, headers=_TV_HEADERS),
            client.post(_TV_SCAN_URL, json=trend_payload, headers=_TV_HEADERS),
            client.post(_TV_SCAN_URL, json=ep_payload, headers=_TV_HEADERS),
            client.post(_TV_SCAN_URL, json=htf_payload, headers=_TV_HEADERS),
            client.post(_TV_SCAN_URL, json=sector_payload, headers=_TV_HEADERS),
            client.post(_TV_SCAN_URL, json=ipo_short_payload, headers=_TV_HEADERS),
        )

    sec_cols = ["name", "close", "change", "change|1W", "change|1M", "change|3M"]
    spy_1m = 0.0
    sectors = []
    for item in r_sectors.json().get("data", []):
        t = item["s"].split(":")[-1]
        d = dict(zip(sec_cols, item["d"]))
        d["ticker"] = t
        if t == "SPY":
            spy_1m = d.get("change|1M") or 0.0
        else:
            sectors.append(d)

    sector_rs: dict = {}
    sector_list = []
    for s in sectors:
        t = s["ticker"]
        chg1m = s.get("change|1M") or 0.0
        chg3m = s.get("change|3M") or 0.0
        rs = round(chg1m - spy_1m, 2)
        sector_rs[t] = rs
        sector_list.append({
            "ticker": t,
            "name": _SECTOR_NAMES.get(t, t),
            "change|1M": round(chg1m, 2),
            "change|3M": round(chg3m, 2),
            "rs_vs_spy": rs,
            "leading": rs > 0,
        })
    sector_list.sort(key=lambda x: x["rs_vs_spy"], reverse=True)

    def enrich_and_filter(stocks, apply_sector_filter=True):
        result = []
        for s in stocks:
            s["rs_vs_spy"] = round((s.get("change|1M") or 0) - spy_1m, 2)
            tv_sec = s.get("sector", "")
            etf = _TV_SECTOR_TO_ETF.get(tv_sec)
            s["sector_etf"] = etf or ""
            s["sector_rs"] = round(sector_rs.get(etf, 0), 2) if etf else None
            s["sector_leading"] = (sector_rs.get(etf, 0) > 0) if etf else None
            if not apply_sector_filter or s["sector_leading"] is not False:
                result.append(s)
        return result

    htf = [
        s for s in _tv_format(r_htf.json())
        if (s.get("close") or 0) >= (s.get("price_52_week_high") or 1) * 0.75
    ]

    def enrich_ipo_shorts(stocks):
        result = []
        for s in stocks:
            if s.get("EMA50") is None or s.get("EMA200") is None:
                continue
            if s["above_ema50"] or s["above_ema200"]:
                continue
            rs = round((s.get("change|1M") or 0) - spy_1m, 2)
            if rs >= 0:
                continue
            s["rs_vs_spy"] = rs
            tv_sec = s.get("sector", "")
            etf = _TV_SECTOR_TO_ETF.get(tv_sec)
            s["sector_etf"] = etf or ""
            s["sector_rs"] = round(sector_rs.get(etf, 0), 2) if etf else None
            result.append(s)
        return result

    return {
        "pre_breakout": enrich_and_filter(_tv_format(r_pre.json())),
        "in_trend": enrich_and_filter(_tv_format(r_trend.json())),
        "episodic_pivots": enrich_and_filter(_tv_format(r_ep.json()), apply_sector_filter=False),
        "high_tight_flags": enrich_and_filter(htf, apply_sector_filter=False),
        "ipo_shorts": enrich_ipo_shorts(_format_ipo_shorts(r_ipo.json())),
        "sectors": sector_list,
        "spy_1m": round(spy_1m, 2),
    }


# ---------------------------------------------------------------------------
# Public — Strength ranking
# ---------------------------------------------------------------------------
async def get_strength() -> dict:
    sector_etfs = ["XLK", "XLF", "XLE", "XLV", "XLY", "XLI", "XLB", "XLC", "XLRE", "XLU", "XLP", "SPY"]
    sector_payload = {
        "symbols": {"tickers": [f"AMEX:{e}" for e in sector_etfs]},
        "columns": ["name", "close", "change", "change|1W", "change|1M", "change|3M"],
    }
    strength_payload = {
        "filter": [
            {"left": "change|1M", "operation": "greater", "right": 3},
            {"left": "volume", "operation": "greater", "right": 400000},
            {"left": "market_cap_basic", "operation": "greater", "right": 500000000},
            {"left": "close", "operation": "greater", "right": 8},
        ],
        "columns": _STRENGTH_COLS,
        "sort": {"sortBy": "change|1M", "sortOrder": "desc"},
        "range": [0, 800],
    }

    async with httpx.AsyncClient(timeout=15) as client:
        r_sec, r_stocks = await asyncio.gather(
            client.post(_TV_SCAN_URL, json=sector_payload, headers=_TV_HEADERS),
            client.post(_TV_SCAN_URL, json=strength_payload, headers=_TV_HEADERS),
        )

    sec_cols = ["name", "close", "change", "change|1W", "change|1M", "change|3M"]
    spy_1m = 0.0
    sector_rs_map: dict = {}
    for item in r_sec.json().get("data", []):
        t = item["s"].split(":")[-1]
        d = dict(zip(sec_cols, item["d"]))
        if t == "SPY":
            spy_1m = d.get("change|1M") or 0.0
        else:
            sector_rs_map[t] = d.get("change|1M") or 0.0
    sector_rs_map = {k: round(v - spy_1m, 2) for k, v in sector_rs_map.items()}

    stocks = []
    for item in r_stocks.json().get("data", []):
        s = dict(zip(_STRENGTH_COLS, item["d"]))
        s["ticker"] = item["s"].split(":")[-1]
        for k, v in list(s.items()):
            if isinstance(v, float):
                s[k] = round(v, 3)
        _classify(s, sector_rs_map, spy_1m)
        stocks.append(s)

    tradable = [s for s in stocks if s["verdict"] == "TRADABLE"]
    shares_worthy = [s for s in stocks if s["verdict"] == "SHARES WORTHY"]

    tradable.sort(key=lambda x: (x.get("quality_score", 0), x.get("rs_vs_spy", 0)), reverse=True)
    shares_worthy.sort(key=lambda x: x.get("change|1M", 0), reverse=True)

    return {
        "tradable": tradable[:20],
        "shares_worthy": shares_worthy[:20],
        "spy_1m": round(spy_1m, 2),
        "total_scanned": len(stocks),
    }
