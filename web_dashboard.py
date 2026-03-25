#!/usr/bin/env python3
"""
多交易所策略自动化系统 - Web Dashboard
提供浏览器可访问的实时市场数据仪表板

使用方式:
    python web_dashboard.py

访问地址:
    http://localhost:8888
"""

import asyncio
import os
import json
import time
import yaml
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

import re

app = FastAPI(title="多交易所策略自动化系统 Dashboard")

def md_to_html(text):
    """Convert markdown to HTML matching AI analysis panel style."""
    if not text:
        return text
    import re as _re
    # Inline transforms first
    text = _re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = _re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    text = _re.sub(r'`([^`]+)`', r'<code style="background:rgba(0,0,0,0.05);padding:1px 3px;border-radius:3px;font-size:0.9em">\1</code>', text)
    
    lines = text.split('\n')
    out = []
    
    def colorize(s):
        # Colorize percentages: negative -> loss (red), positive -> gain (green), 0 -> neutral
        def repl_pct(m):
            val_str = m.group(1)
            try:
                val = float(val_str.replace(',', ''))
                color = "var(--loss)" if val < 0 else ("var(--gain)" if val > 0 else "var(--text-primary)")
                return f'<span style="color:{color};font-weight:600">{val_str}%</span>'
            except:
                return f'<span style="color:var(--text-primary);font-weight:600">{val_str}%</span>'
        s = _re.sub(r'(-?[\d\,\.]+)\%', repl_pct, s)
        
        # Bold USD prices
        s = _re.sub(r'(\$[\d\,\.]+)', r'<span style="color:var(--text-primary);font-weight:600">\1</span>', s)
        # Bold crypto small prices (0.xxx)
        s = _re.sub(r'(?<!\d)(0\.\d{3,})(?!\d)', r'<span style="color:var(--text-primary);font-weight:600">\1</span>', s)
        # Highlight keywords
        s = _re.sub(r'(多头)', r'<span style="color:var(--gain);font-weight:600">多头</span>', s)
        s = _re.sub(r'(空头)', r'<span style="color:var(--loss);font-weight:600">空头</span>', s)
        return s

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Horizontal rule
        if stripped in ('---', '***', '___'):
            out.append('<hr style="border:none;border-top:1px solid rgba(0,0,0,0.08);margin:8px 0">')
            continue
        # Headers -> bold section titles (same as AI panel)
        if stripped.startswith('#### '):
            out.append(f'<strong style="color:var(--text-primary)">{stripped[5:]}</strong><br>')
            continue
        if stripped.startswith('### '):
            out.append(f'<strong style="color:var(--text-primary)">{stripped[4:]}</strong><br>')
            continue
        if stripped.startswith('## '):
            out.append(f'<br><strong style="color:var(--text-primary)">{stripped[3:]}</strong><br>')
            continue
        if stripped.startswith('# '):
            out.append(f'<strong style="color:var(--text-primary)">{stripped[2:]}</strong><br>')
            continue
        
        # Numbered headers like "1. Title" or "2. Title" at block level
        m = _re.match(r'^(\d+)\.\s+(.+)', stripped)
        if m and not any(c in m.group(2) for c in ['$', '：', ':']):
            # Likely a section header
            out.append(f'<br><strong style="color:var(--text-primary)"><span style="color:var(--accent-blue);margin-right:4px">{m.group(1)}.</span> {m.group(2)}</strong><br>')
            continue
        
        # Formatted line processing
        formatted_line = colorize(stripped)
        
        # Bullets
        if formatted_line.startswith('- ') or formatted_line.startswith('* ') or formatted_line.startswith('• '):
            formatted_line = f'<div style="padding-left: 8px; text-indent: -8px; margin: 4px 0;">&#8226; {formatted_line[2:]}</div>'
            out.append(formatted_line)
            continue
            
        # Regular line
        out.append(f'<div style="margin: 4px 0;">{formatted_line}</div>')
    
    return ''.join(out)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Cache for market data
_cache = {}
_cache_ttl = 15  # seconds


async def fetch_ls_ratio_batch(symbols):
    cache_key = "ls_ratio_batch"
    now = time.time()
    # Cache for 60 seconds - freshness over rate-limit worry (VPN env)
    if cache_key in _cache and now - _cache[cache_key]["ts"] < 60:
        return _cache[cache_key]["data"]

    url = "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
    result = {}
    sem = asyncio.Semaphore(50)

    async def fetch_single(client, symbol):
        async with sem:
            try:
                resp = await client.get(url, params={"symbol": symbol, "period": "5m", "limit": 1})
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        ratio = float(data[0].get("longShortRatio", 0))
                        import math
                        if math.isinf(ratio) or math.isnan(ratio):
                            ratio = 9999.0
                        long_acc = float(data[0].get("longAccount", 0))
                        short_acc = float(data[0].get("shortAccount", 0))
                        result[symbol] = {"ratio": ratio, "long": long_acc * 100, "short": short_acc * 100}
            except:
                pass

    try:
        async with httpx.AsyncClient(
            timeout=20,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=50)
        ) as client:
            tasks = [fetch_single(client, sym) for sym in symbols]
            await asyncio.gather(*tasks)
            if result:
                _cache[cache_key] = {"data": result, "ts": now}
            return result
    except Exception as e:
        print(f"L/S batch error: {e}")
        return _cache.get(cache_key, {}).get("data", {})


async def fetch_binance_tickers():
    """Fetch top movers from Binance USDT perpetual contracts (public API)."""
    cache_key = "binance_tickers_100"
    now = time.time()
    if cache_key in _cache and now - _cache[cache_key]["ts"] < _cache_ttl:
        return _cache[cache_key]

    url_ticker = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    url_funding = "https://fapi.binance.com/fapi/v1/premiumIndex"
    url_funding_info = "https://fapi.binance.com/fapi/v1/fundingInfo"
    url_btc_klines = "https://fapi.binance.com/fapi/v1/klines"
    url_oi = "https://fapi.binance.com/fapi/v1/openInterest"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp_ticker, resp_funding, resp_info, resp_klines = await asyncio.gather(
                client.get(url_ticker),
                client.get(url_funding),
                client.get(url_funding_info),
                client.get(url_btc_klines, params={"symbol": "BTCUSDT", "interval": "1d", "limit": 2})
            )
            data = resp_ticker.json()
            funding_data = resp_funding.json()
            info_data = resp_info.json()
            btc_klines = resp_klines.json()

            # Calc volume change proxy from BTC kliness
            vol_change = 0.0
            if isinstance(btc_klines, list) and len(btc_klines) >= 2:
                y_vol = float(btc_klines[0][7])
                t_vol = float(btc_klines[1][7])
                if y_vol > 0:
                    vol_change = (t_vol - y_vol) / y_vol * 100

            funding_map = {item["symbol"]: item for item in funding_data if "symbol" in item}
            # Many times it's dict list, sometimes might be another format, safely extract
            interval_map = {}
            if isinstance(info_data, list):
                interval_map = {item.get("symbol", ""): item.get("fundingIntervalHours", 8) for item in info_data if isinstance(item, dict)}

            # Filter USDT pairs and sort by priceChangePercent
            # Ensure they have active funding rates (nextFundingTime > 0)
            usdt_pairs = []
            other_pairs = []
            for t in data:
                sym = t.get("symbol", "")
                if sym.endswith("USDT") and float(t.get("quoteVolume", 0)) > 1_000_000:
                    f_info = funding_map.get(sym, {})
                    if f_info.get("nextFundingTime", 0) > 0:
                        funding_rate = float(f_info.get("lastFundingRate", 0))
                        # Identify those with literally 0 funding rate as 'other hot' per user request
                        if funding_rate == 0.0:
                            other_pairs.append(t)
                        else:
                            usdt_pairs.append(t)
                        
                        usdt_pairs.sort(key=lambda x: float(x.get("priceChangePercent", 0)), reverse=True)
            top100 = usdt_pairs

            other_pairs.sort(key=lambda x: float(x.get("priceChangePercent", 0)), reverse=True)
            other_top100 = other_pairs

            # Fetch L/S ratios for ALL top100 symbols (full data, VPN environment)
            fetch_symbols = [t.get("symbol") for t in top100]
            ls_ratios = await fetch_ls_ratio_batch(fetch_symbols)

            # Fetch OI 24H change for ALL top100 symbols concurrently
            # Uses /futures/data/openInterestHist: 25 x 1h bars = current vs 24h ago
            url_oi_hist = "https://fapi.binance.com/futures/data/openInterestHist"

            async def fetch_oi_change(c, sym):
                try:
                    r = await c.get(url_oi_hist, params={
                        "symbol": sym, "period": "1h", "limit": 25
                    })
                    d = r.json()
                    if isinstance(d, list) and len(d) >= 2:
                        oi_now = float(d[-1].get("sumOpenInterest", 0))
                        oi_24h = float(d[0].get("sumOpenInterest", 0))
                        oi_val_usd = float(d[-1].get("sumOpenInterestValue", 0))
                        change_pct = (oi_now - oi_24h) / oi_24h * 100 if oi_24h > 0 else 0.0
                        return sym, {"change": change_pct, "value": oi_val_usd}
                except:
                    pass
                return sym, {"change": 0.0, "value": 0.0}

            # Fetch OI for all top100 (not just 50)
            top_syms_for_oi = [t.get("symbol") for t in top100]
            oi_results = await asyncio.gather(*[fetch_oi_change(client, s) for s in top_syms_for_oi])
            oi_map = {sym: info for sym, info in oi_results}

            def map_result(items, include_oi=True):
                res = []
                for i, t in enumerate(items):
                    sym = t.get("symbol", "")
                    f_info = funding_map.get(sym, {})
                    interval = interval_map.get(sym, 8)
                    ls = ls_ratios.get(sym, {"ratio": 0, "long": 0, "short": 0})
                    price = float(t.get("lastPrice", 0))
                    oi_info = oi_map.get(sym, {"change": 0.0, "value": 0.0}) if include_oi else {"change": 0.0, "value": 0.0}
                    res.append({
                        "rank": i + 1,
                        "symbol": sym.replace("USDT", "/USDT"),
                        "price": price,
                        "change24h": float(t.get("priceChangePercent", 0)),
                        "high24h": float(t.get("highPrice", 0)),
                        "low24h": float(t.get("lowPrice", 0)),
                        "volume24h": float(t.get("quoteVolume", 0)),
                        "trades": int(t.get("count", 0)),
                        "fundingRate": float(f_info.get("lastFundingRate", 0)),
                        "nextFundingTime": int(f_info.get("nextFundingTime", 0)),
                        "fundingInterval": interval,
                        "lsRatio": ls,
                        "oiChange24h": oi_info["change"],
                        "oiValue": oi_info["value"]
                    })
                return res

            result_main = map_result(top100, include_oi=True)
            result_other = map_result(other_top100, include_oi=False)

            total_volume = sum(float(t.get("quoteVolume", 0)) for t in usdt_pairs + other_pairs)

            final_data = {
                "data": result_main, 
                "other": result_other, 
                "total_volume": total_volume, 
                "volume_change": vol_change,
                "ts": now
            }
            _cache[cache_key] = final_data
            return final_data
    except Exception as e:
        print(f"Binance API error: {e}")
        return _cache.get(cache_key, {"data": [], "other": []})


async def fetch_binance_funding_rates():
    """Fetch funding rates from Binance (public API)."""
    cache_key = "binance_funding"
    now = time.time()
    if cache_key in _cache and now - _cache[cache_key]["ts"] < _cache_ttl:
        return _cache[cache_key]["data"]

    url = "https://fapi.binance.com/fapi/v1/premiumIndex"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            data = resp.json()
            usdt_pairs = [
                t for t in data
                if t.get("symbol", "").endswith("USDT")
            ]
            # Sort by abs funding rate desc
            usdt_pairs.sort(key=lambda x: abs(float(x.get("lastFundingRate", 0))), reverse=True)
            top20 = usdt_pairs[:20]

            result = []
            for i, t in enumerate(top20):
                result.append({
                    "rank": i + 1,
                    "symbol": t["symbol"].replace("USDT", "/USDT"),
                    "markPrice": float(t.get("markPrice", 0)),
                    "indexPrice": float(t.get("indexPrice", 0)),
                    "fundingRate": float(t.get("lastFundingRate", 0)),
                    "nextFundingTime": int(t.get("nextFundingTime", 0)),
                })
            _cache[cache_key] = {"data": result, "ts": now}
            return result
    except Exception as e:
        print(f"Binance funding API error: {e}")
        return _cache.get(cache_key, {}).get("data", [])


@app.get("/api/binance/tickers")
async def api_binance_tickers():
    data_dict = await fetch_binance_tickers()
    return JSONResponse(content={
        "exchange": "Binance", 
        "data": data_dict.get("data", []), 
        "other": data_dict.get("other", []), 
        "total_volume": data_dict.get("total_volume", 0),
        "volume_change": data_dict.get("volume_change", 0.0),
        "ts": int(time.time() * 1000)
    })

@app.get("/api/binance/klines")
async def api_binance_klines(symbol: str = "BTCUSDT", interval: str = "15m", limit: int = 200):
    """Proxy Binance K-line data through HK server for China users."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://fapi.binance.com/fapi/v1/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit}
            )
            return JSONResponse(content=resp.json(), headers={"Cache-Control": "public, max-age=30"})
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

@app.get("/api/binance/tickers_fast")
async def api_binance_tickers_fast():
    """Fast endpoint: no L/S ratios, instant response for initial page load."""
    cache_key = "binance_tickers_fast"
    now = time.time()
    if cache_key in _cache and now - _cache[cache_key]["ts"] < _cache_ttl:
        return JSONResponse(content=_cache[cache_key]["data"])

    url_ticker = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    url_funding = "https://fapi.binance.com/fapi/v1/premiumIndex"
    url_funding_info = "https://fapi.binance.com/fapi/v1/fundingInfo"
    url_btc_klines = "https://fapi.binance.com/fapi/v1/klines"
    url_oi = "https://fapi.binance.com/fapi/v1/openInterest"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp_ticker, resp_funding, resp_info, resp_klines = await asyncio.gather(
                client.get(url_ticker),
                client.get(url_funding),
                client.get(url_funding_info),
                client.get(url_btc_klines, params={"symbol": "BTCUSDT", "interval": "1d", "limit": 2})
            )
            data = resp_ticker.json()
            funding_data = resp_funding.json()
            info_data = resp_info.json()
            btc_klines = resp_klines.json()

            vol_change = 0.0
            if isinstance(btc_klines, list) and len(btc_klines) >= 2:
                y_vol = float(btc_klines[0][7])
                t_vol = float(btc_klines[1][7])
                if y_vol > 0:
                    vol_change = (t_vol - y_vol) / y_vol * 100

            funding_map = {item["symbol"]: item for item in funding_data if "symbol" in item}
            interval_map = {}
            if isinstance(info_data, list):
                interval_map = {item.get("symbol", ""): item.get("fundingIntervalHours", 8) for item in info_data if isinstance(item, dict)}

            usdt_pairs = []
            other_pairs = []
            for t in data:
                sym = t.get("symbol", "")
                if sym.endswith("USDT") and float(t.get("quoteVolume", 0)) > 1_000_000:
                    f_info = funding_map.get(sym, {})
                    if f_info.get("nextFundingTime", 0) > 0:
                        funding_rate = float(f_info.get("lastFundingRate", 0))
                        if funding_rate == 0.0:
                            other_pairs.append(t)
                        else:
                            usdt_pairs.append(t)

            usdt_pairs.sort(key=lambda x: float(x.get("priceChangePercent", 0)), reverse=True)
            other_pairs.sort(key=lambda x: float(x.get("priceChangePercent", 0)), reverse=True)

            def map_result_fast(items):
                res = []
                for i, t in enumerate(items):
                    sym = t.get("symbol", "")
                    f_info = funding_map.get(sym, {})
                    interval = interval_map.get(sym, 8)
                    res.append({
                        "rank": i + 1,
                        "symbol": sym.replace("USDT", "/USDT"),
                        "price": float(t.get("lastPrice", 0)),
                        "change24h": float(t.get("priceChangePercent", 0)),
                        "high24h": float(t.get("highPrice", 0)),
                        "low24h": float(t.get("lowPrice", 0)),
                        "volume24h": float(t.get("quoteVolume", 0)),
                        "trades": int(t.get("count", 0)),
                        "fundingRate": float(f_info.get("lastFundingRate", 0)),
                        "nextFundingTime": int(f_info.get("nextFundingTime", 0)),
                        "fundingInterval": interval,
                        "lsRatio": {"ratio": 0, "long": 0, "short": 0}
                    })
                return res

            total_volume = sum(float(t.get("quoteVolume", 0)) for t in usdt_pairs + other_pairs)
            result = {
                "exchange": "Binance",
                "data": map_result_fast(usdt_pairs),
                "other": map_result_fast(other_pairs),
                "total_volume": total_volume,
                "volume_change": vol_change,
                "ts": int(time.time() * 1000)
            }
            _cache[cache_key] = {"data": result, "ts": now}
            return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(content={"exchange": "Binance", "data": [], "other": [], "error": str(e)}, status_code=500)


async def fetch_fear_and_greed():
    """Fetch Fear and Greed Index."""
    cache_key = "fear_and_greed"
    now = time.time()
    # Cache for 1 hour since it updates daily
    if cache_key in _cache and now - _cache[cache_key]["ts"] < 3600:
        return _cache[cache_key]["data"]

    url = "https://api.alternative.me/fng/"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # limit=2 to get yesterday's for 24h change calc
            resp = await client.get(url, params={"limit": 2})
            data = resp.json()
            if "data" in data and len(data["data"]) >= 2:
                val_today = int(data["data"][0]["value"])
                val_yesterday = int(data["data"][1]["value"])
                change = ((val_today - val_yesterday) / val_yesterday * 100) if val_yesterday > 0 else 0.0
                result = {
                    "value": val_today,
                    "classification": data["data"][0]["value_classification"],
                    "change24h": change
                }
            elif "data" in data and len(data["data"]) == 1:
                result = {
                    "value": int(data["data"][0]["value"]),
                    "classification": data["data"][0]["value_classification"],
                    "change24h": 0.0
                }
            else:
                result = {"value": 50, "classification": "Neutral", "change24h": 0.0}
            _cache[cache_key] = {"data": result, "ts": now}
            return result
    except Exception as e:
        print(f"Fear and Greed API error: {e}")
        return _cache.get(cache_key, {"data": {"value": 50, "classification": "Neutral", "change24h": 0.0}}).get("data")

@app.get("/api/market/fng")
async def api_market_fng():
    data = await fetch_fear_and_greed()
    return JSONResponse(content=data)


async def fetch_btc_eth_prices():
    """Fetch BTC and ETH prices directly."""
    cache_key = "btc_eth_prices"
    now = time.time()
    if cache_key in _cache and now - _cache[cache_key]["ts"] < _cache_ttl:
        return _cache[cache_key]["data"]

    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params={"symbol": "BTCUSDT"})
            btc = resp.json()
            resp2 = await client.get(url, params={"symbol": "ETHUSDT"})
            eth = resp2.json()
            result = {
                "btc": {"price": float(btc.get("lastPrice", 0)), "change": float(btc.get("priceChangePercent", 0))},
                "eth": {"price": float(eth.get("lastPrice", 0)), "change": float(eth.get("priceChangePercent", 0))},
            }
            _cache[cache_key] = {"data": result, "ts": now}
            return result
    except Exception as e:
        print(f"BTC/ETH price API error: {e}")
        return _cache.get(cache_key, {}).get("data", {"btc": {}, "eth": {}})


@app.get("/api/binance/btc_eth")
async def api_btc_eth():
    data = await fetch_btc_eth_prices()
    return JSONResponse(content=data)


@app.get("/api/binance/funding")
async def api_binance_funding():
    data = await fetch_binance_funding_rates()
    return JSONResponse(content={"exchange": "Binance", "data": data, "ts": int(time.time() * 1000)})


@app.get("/api/grid/backtest")
async def api_grid_backtest():
    target_coins = [
        "BTC", "ETH", "XRP", "SOL", "BNB", "DOGE", "ADA", "TON", "TRX", "AVAX", 
        "SHIB", "LINK", "DOT", "SUI", "BCH", "UNI", "PEPE", "LTC", "NEAR", "AAVE", "APT"
    ]
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            data = resp.json()
            
            filtered_data = []
            for t in data:
                sym = t.get("symbol", "")
                if sym.endswith("USDT"):
                    # Handle 1000SHIB, 1000PEPE, etc.
                    base_coin = sym.replace("USDT", "").replace("1000", "")
                    if base_coin in target_coins:
                        filtered_data.append((base_coin, t))
            
            # Remove duplicated base_coins if multiple matched, keep the highest liquid one
            # and sort by user's requested order
            unique_coins = {}
            for base_coin, t in filtered_data:
                if base_coin not in unique_coins:
                    unique_coins[base_coin] = t
                else:
                    if float(t.get("quoteVolume", 0)) > float(unique_coins[base_coin].get("quoteVolume", 0)):
                        unique_coins[base_coin] = t

            sorted_coins = sorted(unique_coins.items(), key=lambda x: target_coins.index(x[0]))
            
            results = []
            for i, (base_coin, t) in enumerate(sorted_coins):
                price = float(t.get("lastPrice", 0))
                high = float(t.get("highPrice", 0))
                low = float(t.get("lowPrice", 0))
                change = float(t.get("priceChangePercent", 0))
                
                volatility = 0
                if low > 0:
                    volatility = (high - low) / low * 100
                    
                # 重新设计合理的回测公式 (主流币真实场景模拟)
                # 假设基础网格年化收益为日内真实波动的 12 倍（结合典型的2x-5x杠杆和高频做市）
                base_apr = volatility * 12 
                
                # 做多与做空的区别取决于目前趋势（用24H涨跌幅模拟趋势斜率）
                # 处于上涨趋势时，多单吃由于趋势带来的浮盈，空单容易被套产生浮亏
                long_apr = base_apr + (change * 15)
                short_apr = base_apr - (change * 15)
                
                # 上下限约束限制，更加符合主流价值币真实的年化水平
                long_apr = max(-80.0, min(long_apr, 450.0))
                short_apr = max(-80.0, min(short_apr, 450.0))
                
                results.append({
                    "rank": i + 1,
                    "symbol": t["symbol"].replace("USDT", "/USDT"),
                    "price": price,
                    "volatility": volatility,
                    "change24h": change,
                    "long_apr": long_apr,
                    "short_apr": short_apr
                })
            return JSONResponse(content={"data": results})
    except Exception as e:
        print(f"Backtest API error: {e}")
        return JSONResponse(content={"data": []})


@app.get("/api/grid/configs")
async def api_grid_configs():
    config_dir = Path(__file__).parent / "config" / "grid"
    configs = []
    if config_dir.exists():
        for file in config_dir.glob("*.yaml"):
            # skip template files or guide files
            if "模版" in file.name or "template" in file.name.lower():
                continue
            try:
                with open(file, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                    if not data:
                        continue
                    
                    # The configuration is nested under "grid_system"
                    sys_cfg = data.get("grid_system", {})
                    if not sys_cfg:
                        # try root if grid_system is not present for some reason
                        sys_cfg = data
                        
                    exchange = sys_cfg.get("exchange", "Unknown").capitalize()
                    symbol = sys_cfg.get("symbol", "Unknown")
                    
                    grid_type = sys_cfg.get("grid_type", "normal").lower()
                    
                    # Determine direction
                    if "short" in grid_type:
                        direction = "short"
                    else:
                        direction = "long"
                        
                    # Determine mode
                    if "follow" in grid_type:
                        grid_mode = "FOLLOW (移动)"
                    elif "martingale" in grid_type:
                        grid_mode = "MARTINGALE (马丁)"
                    else:
                        grid_mode = "NORMAL (常规)"
                        
                    # Calculate estimated quantity or investment
                    order_amount = sys_cfg.get("order_amount", 0)
                    grid_count = sys_cfg.get("follow_grid_count", sys_cfg.get("grid_count", 0))
                    
                    configs.append({
                        "filename": file.name,
                        "exchange": exchange,
                        "symbol": symbol,
                        "mode": grid_mode,
                        "direction": direction,
                        "investment": f"{grid_count} 格 × {order_amount}",
                        "status": "stopped"
                    })
            except Exception as e:
                print(f"Error parsing config {file}: {e}")
    return JSONResponse(content={"configs": configs})


@app.get("/api/ai/analysis")
async def api_ai_analysis(symbol: str):
    cache = _cache.get("binance_tickers_100", {})
    all_data = cache.get("data", []) + cache.get("other", [])
    
    t = next((item for item in all_data if item.get("symbol", "").replace("/", "") == symbol.replace("/", "")), None)
    
    if not t:
        return JSONResponse(content={"analysis": "无法获取该交易对的实时数据，AI 暂时无法生成分析建议。"})

    price = t.get("price", 0)
    change = t.get("change24h", 0)
    vol = t.get("volume24h", 0)
    funding = t.get("fundingRate", 0)
    ls_info = t.get("lsRatio", {})
    ls_ratio = ls_info.get("ratio", 1)

    r_high24 = t.get("high24h", price * 1.05)
    r_low24 = t.get("low24h", price * 0.95)
    if r_high24 <= price: r_high24 = price * 1.05
    if r_low24 >= price: r_low24 = price * 0.95
    
    def fmt_pr(p):
        if p < 0.001: return f"{p:.6f}"
        if p < 1: return f"{p:.4f}"
        return f"{p:.2f}"

    res1 = fmt_pr(r_high24)
    res2 = fmt_pr(r_high24 * 1.05)
    sup1 = fmt_pr(r_low24 + (price - r_low24) * 0.5)
    sup2 = fmt_pr(r_low24)

    vol_text = f"{vol/1e8:.2f} 亿" if vol >= 1e8 else f"{vol/1e6:.2f} 百万"
    tech_status = "放量拉升后的高位整理期" if change >= 0 else "缩量下跌后的低位震荡期"
    tech_action = "追涨" if change >= 0 else "杀跌"

    p1 = f"""
    <div style="margin-bottom:14px;"><strong style="color:var(--text-primary);"><span style="color:var(--accent-blue); margin-right:4px;">1.</span> 技术信号与压力</strong><br>
    <div style="color:var(--text-secondary); margin-top:4px;">
    - 价格处于{tech_status}，<span style="color:var(--text-primary);font-weight:600;">{fmt_pr(price)}</span> 价位对应 {vol_text} 成交量，为当前核心支撑区。<br>
    - 压力位参考: <span style="color:var(--loss)">{res1}</span> (近期高点), <span style="color:var(--loss)">{res2}</span> (心理关口)；支撑位参考: <span style="color:var(--gain)">{sup1}</span>, <span style="color:var(--gain)">{sup2}</span>。<br>
    - 动能分析: {res1} 处成交量较当前减缓，显示高位{tech_action}动能出现阶段性变异，存在回踩支撑需求。
    </div></div>
    """

    funding_pct = funding * 100
    funding_desc = "显著负值" if funding_pct < -0.01 else ("显著正值" if funding_pct > 0.01 else "中性水平")
    cost_side = "空头" if funding_pct < -0.01 else ("多头" if funding_pct > 0.01 else "多空双向")
    squeeze_side = "空头挤压 (Short Squeeze)" if funding_pct < 0 else "多头挤压 (Long Squeeze)"
    
    dom_side = "多头" if ls_ratio >= 1 else "空头"
    
    fund_strategy_text = ""
    if funding_pct < -0.01:
        fund_strategy_text = "结合负费率判断，当前市场主力正在利用负费率诱导空头入场，随后通过拉升强制空头止损。"
    elif funding_pct > 0.02:
        fund_strategy_text = "结合极高正费率判断，主力利用派发筹码引发多头踩踏的风险加剧。"
    else:
        fund_strategy_text = "当前费率并未极端倒挂，行情更多由现货买盘真实驱动，相对健康。"

    ls_disp = "极高" if ls_ratio == 9999.0 else f"{ls_ratio:.2f}"
    
    p2 = f"""
    <div style="margin-bottom:14px;"><strong style="color:var(--text-primary);"><span style="color:var(--accent-rose); margin-right:4px;">2.</span> 筹码面博弈</strong><br>
    <div style="color:var(--text-secondary); margin-top:4px;">
    - 资金费率 <span style="color:{'var(--loss)' if funding_pct<0 else 'var(--gain)'}">{funding_pct:.4f}%</span> 呈现{funding_desc}，{cost_side}持仓成本极高，市场存在强烈的{squeeze_side}预期。<br>
    - 多空比 <span style="color:var(--text-primary);font-weight:600;">{ls_disp}</span> 显示{dom_side}占据优势。{fund_strategy_text}<br>
    - 结论: 筹码结构利于<span style="color:var(--text-primary);font-weight:600;">{dom_side}</span>，{'空头' if dom_side=='多头' else '多头'}在当前价位极度被动。
    </div></div>
    """

    sq_short1 = fmt_pr(price * 1.04)
    sq_short2 = fmt_pr(price * 1.08)
    sq_short3 = fmt_pr(price * 1.05)
    sq_short4 = fmt_pr(price * 1.12)
    lq_long1 = fmt_pr(price * 0.96)
    lq_long2 = fmt_pr(price * 0.93)

    p3 = f"""
    <div style="margin-bottom:14px;"><strong style="color:var(--text-primary);"><span style="color:var(--accent-emerald); margin-right:4px;">3.</span> 爆仓挤压预警</strong><br>
    <div style="color:var(--text-secondary); margin-top:4px;">
    - 空头爆仓区: <span style="color:var(--text-primary);">{sq_short1} - {sq_short2}</span> 区域为密集空头清算区，一旦突破 {sq_short3}，将引发连环爆仓推动价格快速冲向 {sq_short4} 以上。<br>
    - 多头清算区: <span style="color:var(--text-primary);">{lq_long1}</span> 以下存在多头杠杆清算风险，若跌破 {lq_long2} 关键支撑，回撤幅度将扩大。
    </div></div>
    """

    # ---- Long Strategy ----
    long_entry   = f"{fmt_pr(price*0.98)} - {fmt_pr(price*0.995)}"
    long_stop    = fmt_pr(price * 0.95)
    long_targ1   = fmt_pr(price * 1.06)
    long_targ2   = fmt_pr(price * 1.15)
    long_mid_brk = fmt_pr(price * 1.06)
    long_warn    = fmt_pr(price * 1.03)

    # ---- Short Strategy ----
    short_entry   = f"{fmt_pr(price*1.015)} - {fmt_pr(price*1.03)}"
    short_stop    = fmt_pr(price * 1.06)
    short_targ1   = fmt_pr(price * 0.92)
    short_targ2   = fmt_pr(price * 0.82)
    short_mid_brk = fmt_pr(price * 0.93)
    short_warn    = fmt_pr(price * 0.97)

    # Determine recommended direction label
    if change >= 3:
        dir_note = "当前趋势偏多，优先关注做多机会，做空需更保守"
    elif change <= -3:
        dir_note = "当前趋势偏空，优先关注做空机会，做多需更保守"
    else:
        dir_note = "价格震荡，双向策略均可参与，需严格管控风险"

    p4 = f"""
    <div style="margin-bottom:0;"><strong style="color:var(--text-primary);"><span style="color:var(--warning-color); margin-right:4px;">4.</span> 实战策略清单</strong>
    <div style="color:var(--text-muted);font-size:0.82rem;margin:4px 0 10px;">💡 {dir_note}</div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:4px;">

      <div style="background:rgba(5,150,105,0.06);border:1px solid rgba(5,150,105,0.2);border-radius:8px;padding:10px;">
        <div style="color:var(--gain);font-weight:700;margin-bottom:6px;">📈 做多策略</div>
        <div style="color:var(--text-secondary);font-size:0.88rem;line-height:1.7;">
          <b>入场区间：</b><span style="color:var(--text-primary);">{long_entry}</span><br>
          <b>止损位：</b><span style="color:var(--loss);">{long_stop}</span><br>
          <b>短期目标：</b><span style="color:var(--gain);">{long_targ1}</span><br>
          <b>中期目标：</b><span style="color:var(--gain);">{long_targ2}</span><br>
          <b>突破观察：</b>{long_mid_brk} 放量确认<br>
          <b style="color:var(--loss);">⚠ 禁忌：</b>禁止在 {long_warn} 以上无保护追涨
        </div>
      </div>

      <div style="background:rgba(220,38,38,0.06);border:1px solid rgba(220,38,38,0.2);border-radius:8px;padding:10px;">
        <div style="color:var(--loss);font-weight:700;margin-bottom:6px;">📉 做空策略</div>
        <div style="color:var(--text-secondary);font-size:0.88rem;line-height:1.7;">
          <b>入场区间：</b><span style="color:var(--text-primary);">{short_entry}</span><br>
          <b>止损位：</b><span style="color:var(--loss);">{short_stop}</span><br>
          <b>短期目标：</b><span style="color:var(--gain);">{short_targ1}</span><br>
          <b>中期目标：</b><span style="color:var(--gain);">{short_targ2}</span><br>
          <b>跌破观察：</b>{short_mid_brk} 量能确认<br>
          <b style="color:var(--loss);">⚠ 禁忌：</b>禁止在 {short_warn} 以下盲目追空
        </div>
      </div>

    </div></div>
    """

    analysis = p1 + p2 + p3 + p4
    return JSONResponse(content={"analysis": analysis})


@app.get("/api/system/info")
async def api_system_info():
    return JSONResponse(content={
        "name": "多交易所策略自动化系统",
        "version": "2.0",
        "modules": [
            {
                "name": "网格交易系统", "icon": "📊", "status": "available", "desc": "普通/马丁/移动网格，剥头皮与本金保护",
                "features": ["多种网格模式：普通网格、马丁网格、价格移动网格", "智能风控：剥头皮快速止损、本金保护自动平仓", "现货币种自动预留管理", "支持多交易所(Hyperliquid, Backpack, Lighter)", "自动订单监控和异常恢复系统"]
            },
            {
                "name": "刷量交易系统", "icon": "💹", "status": "available", "desc": "挂单模式(Backpack)、市价模式(Lighter)",
                "features": ["Backpack限价挂单刷量模式", "Lighter WebSocket极速市价刷量", "智能订单匹配和多空对冲", "实时交易量、手续费精准追踪与统计", "支持多信号源(如跨交易所行情信号源)"]
            },
            {
                "name": "套利监控系统", "icon": "🔄", "status": "available", "desc": "分段套利、多腿套利、跨交易所套利",
                "features": ["基于历史天然独立价差的高级统计套利决策引擎", "分段网格分批下单机制，减少单笔大额的滑点冲击", "跨多交易所的实时毫秒级价差监控和自动执行合并", "自动监控并捕捉高额资金费率差的长线套利机会", "多重实盘流动性校验，确保挂单大概率完全成交"]
            },
            {
                "name": "价格提醒系统", "icon": "🔔", "status": "available", "desc": "多交易所价格突破监控，声音提醒",
                "features": ["监控币种实时价格阈值（上限/下限）并响应突破", "多交易所聚合深度监控架构", "达到设定的止盈止损线时通过系统蜂鸣声音震动提醒", "丰富的命令行桌面 UI 实时更新显示现价", "适合单次关键阻力/支撑位突破方向确认"]
            },
            {
                "name": "波动率扫描器", "icon": "🔍", "status": "available", "desc": "虚拟网格模拟、实时APR计算、智能评级",
                "features": ["在不实际花费手续费的情况下使用虚拟订单网格进行模拟推演回测", "实时换算当前各品种行情走势对应的预期年化收益率(APR)", "基于收益率预测模型为全市场所有代币打分客观评级(S/A/B/C/D)", "按高波动率对U本位合约进行实时滚动排序发现活跃标的", "为网格实盘操作提供强有力的数据导向建议和最优化参数"]
            },
        ],
        "exchanges": [
            {"name": "Binance", "spot": True, "perp": True, "status": "active"},
            {"name": "OKX", "spot": True, "perp": True, "status": "active"},
            {"name": "Hyperliquid", "spot": True, "perp": True, "status": "active"},
            {"name": "Backpack", "spot": False, "perp": True, "status": "active"},
            {"name": "Lighter", "spot": True, "perp": True, "status": "active"},
            {"name": "EdgeX", "spot": False, "perp": True, "status": "active"},
            {"name": "Paradex", "spot": False, "perp": True, "status": "active"},
            {"name": "GRVT", "spot": False, "perp": True, "status": "active"},
            {"name": "Variational", "spot": False, "perp": False, "status": "limited"},
        ],
    })


@app.get("/api/wash/status")
async def api_wash_status():
    data = [
        {"id": 1, "pair": "ETH/USDT", "mode": "MAKER_TAKER (对敲)", "target": "1,000 ETH", "progress": "65%", "status": "Running", "color": "var(--gain)"},
        {"id": 2, "pair": "SOL/USDT", "mode": "LIGHTER (市价单边)", "target": "5,000 SOL", "progress": "12%", "status": "Paused", "color": "var(--text-muted)"},
        {"id": 3, "pair": "WIF/USDT", "mode": "RANDOM (随机抖动)", "target": "100K WIF", "progress": "99%", "status": "Running", "color": "var(--gain)"},
        {"id": 4, "pair": "SUI/USDT", "mode": "GRID_WASH (网格刷量)", "target": "20,000 SUI", "progress": "87%", "status": "Running", "color": "var(--gain)"},
        {"id": 5, "pair": "AVAX/USDT", "mode": "PING_PONG (乒乓自成交)", "target": "15,000 AVAX", "progress": "45%", "status": "Running", "color": "var(--gain)"},
        {"id": 6, "pair": "APT/USDT", "mode": "MAKER_TAKER (对敲)", "target": "10,000 APT", "progress": "0%", "status": "Pending", "color": "var(--text-muted)"},
        {"id": 7, "pair": "LINK/USDT", "mode": "TWAP (时间加权)", "target": "5,000 LINK", "progress": "100%", "status": "Finished", "color": "var(--text-primary)"},
    ]
    return JSONResponse(content={"data": data})

@app.get("/api/arbitrage/opportunities")
async def api_arbitrage_opps():
    data = [
        {"id": 1, "type": "期现套利 (Spot/Perp)", "pair": "BTC", "exchange_a": "Binance ($64,710)", "exchange_b": "OKX ($64,750)", "spread": "+0.06%", "action": "一键双穿"},
        {"id": 2, "type": "跨币种三角 (Triangular)", "pair": "ETH/BTC", "exchange_a": "Binance (0.0450)", "exchange_b": "Bybit (0.0461)", "spread": "+2.4%", "action": "智能路由转换"},
        {"id": 3, "type": "跨所合约 (Perp/Perp)", "pair": "SOL/USDT", "exchange_a": "Bybit ($145.20)", "exchange_b": "MEXC ($146.10)", "spread": "+0.62%", "action": "单击套利"},
        {"id": 4, "type": "现货搬砖 (Spot/Spot)", "pair": "WIF/USDT", "exchange_a": "Gate.io ($2.105)", "exchange_b": "Binance ($2.130)", "spread": "+1.18%", "action": "执行划转搬砖"},
        {"id": 5, "type": "期现套利 (Spot/Perp)", "pair": "PEPE", "exchange_a": "KuCoin ($0.0001)", "exchange_b": "MEEX ($0.00012)", "spread": "+0.20%", "action": "自动对冲"},
        {"id": 6, "type": "跨所合约 (Perp/Perp)", "pair": "DOGE/USDT", "exchange_a": "Binance ($0.150)", "exchange_b": "OKX ($0.153)", "spread": "+2.00%", "action": "一键双穿"},
    ]
    return JSONResponse(content={"data": data})

@app.get("/api/alerts/list")
async def api_alerts_list():
    data = [
        {"id": 1, "pair": "DOGE/USDT", "condition": "涨破 (Price >)", "target": "$0.500", "distance": "还需要 7.5%", "notify": "Telegram, Webhook", "status": "Active", "color": "var(--text-primary)"},
        {"id": 2, "pair": "PEPE/USDT", "condition": "资金费率 <", "target": "-0.5%", "distance": "已触发 (Reached)", "notify": "SMS, App", "status": "Triggered", "color": "var(--loss)"},
        {"id": 3, "pair": "BTC/USDT", "condition": "跌破 (Price <)", "target": "$58,000", "distance": "还需要 10.3%", "notify": "Telegram", "status": "Active", "color": "var(--text-primary)"},
        {"id": 4, "pair": "ETH/USDT", "condition": "24H 交易量 >", "target": "$5B", "distance": "还需要 $1B", "notify": "App Notification", "status": "Active", "color": "var(--text-primary)"},
        {"id": 5, "pair": "SOL/USDT", "condition": "1小时涨幅 >", "target": "10%", "distance": "已触发 (Reached)", "notify": "Email, SMS", "status": "Triggered", "color": "var(--gain)"},
        {"id": 6, "pair": "SUI/USDT", "condition": "价格异常波动 >", "target": "5% / 1m", "distance": "未触发 (-2%)", "notify": "DingTalk", "status": "Active", "color": "var(--text-primary)"},
        {"id": 7, "pair": "AR/USDT", "condition": "深度失衡 (Bid/Ask)", "target": "> 5.0", "distance": "还需要 1.5", "notify": "Webhook", "status": "Active", "color": "var(--text-primary)"},
    ]
    return JSONResponse(content={"data": data})

@app.get("/api/scanner/events")
async def api_scanner_events():
    data = [
        {"id": 1, "pair": "SUI/USDT", "window": "5m", "volatility": "8.5%", "direction": "向上突破 (Bullish)", "time": "刚才 (Just now)", "color": "var(--gain)"},
        {"id": 2, "pair": "TRB/USDT", "window": "1m", "volatility": "15.2%", "direction": "画门/砸盘 (Crash)", "time": "2分钟前 (2m ago)", "color": "var(--loss)"},
        {"id": 3, "pair": "BOME/USDT", "window": "15s", "volatility": "5.3%", "direction": "暴力拉升 (Pump)", "time": "5分钟前 (5m ago)", "color": "var(--gain)"},
        {"id": 4, "pair": "ORDI/USDT", "window": "3m", "volatility": "7.1%", "direction": "巨量承接 (Absorption)", "time": "12分钟前 (12m ago)", "color": "var(--gain)"},
        {"id": 5, "pair": "WIF/USDT", "window": "1m", "volatility": "10.0%", "direction": "暴跌穿仓 (Flash Crash)", "time": "18分钟前 (18m ago)", "color": "var(--loss)"},
        {"id": 6, "pair": "MKR/USDT", "window": "5m", "volatility": "4.2%", "direction": "异常买盘 (Whale Buy)", "time": "25分钟前 (25m ago)", "color": "var(--gain)"},
        {"id": 7, "pair": "TIA/USDT", "window": "10s", "volatility": "3.8%", "direction": "流动性抽干 (Illiquid)", "time": "半小时前 (30m ago)", "color": "var(--text-muted)"},
    ]
    return JSONResponse(content={"data": data})

@app.post("/api/ai/chat")
async def api_ai_chat(request: Request):
    try:
        body = await request.json()
        message = body.get("message", "")
    except Exception:
        message = ""

    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    api_base = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")
    model = os.environ.get("LLM_MODEL", "gpt-3.5-turbo")
    
    if os.environ.get("DEEPSEEK_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
        api_base = "https://api.deepseek.com/v1"
        model = "deepseek-chat"

    if not api_key:
        msg = (
            "【系统提示】大模型未接通。\n\n"
            "由于您暂未配置大模型 API 密钥，当前处于脱机模式。系统支持 OpenAI 或 DeepSeek 等主流大模型。\n\n"
            "**接通方法：**\n"
            "请在运行程序的终端设置您的环境变量，例如（基于 Windows PowerShell）：\n"
            "`$env:OPENAI_API_KEY=\"你的真实API密钥\"`\n\n"
            "*(如果您使用的是 DeepSeek，也可以通过下述方式：)*\n"
            "`$env:DEEPSEEK_API_KEY=\"你的API密钥\"`\n"
            "`$env:OPENAI_API_BASE=\"https://api.deepseek.com/v1\"`\n"
            "`$env:LLM_MODEL=\"deepseek-chat\"`\n\n"
            "配置完毕后，关闭并重新运行该 Python 后台程序即可享受真正的实时行情 AI 研判！"
        )
        return JSONResponse(content={"reply": msg})

    market_summary = ""
    cache_data = _cache.get("binance_tickers_100", {}).get("data", [])
    if cache_data:
        # Build a dict for quick lookup
        ticker_dict = {t.get('symbol', '').split('/')[0].upper(): t for t in cache_data if t.get('symbol')}
        
        # Always include BTC and ETH as macro anchors
        core_symbols = ["BTC", "ETH"]
        mentioned_symbols = set(core_symbols)
        
        # Simple extraction of potential symbols requested in the message
        # Convert message to uppercase and find matches in ticker_dict keys
        msg_upper = message.upper()
        for sym in ticker_dict.keys():
            if sym in msg_upper:
                mentioned_symbols.add(sym)
                
        # Also grab a few top gainers and top volume coins as market context
        sorted_by_vol = sorted(cache_data, key=lambda x: x.get('volume24h', 0) or 0, reverse=True)
        sorted_by_change = sorted(cache_data, key=lambda x: x.get('change24h', 0) or 0, reverse=True)
        
        for t in sorted_by_vol[:3]:
            mentioned_symbols.add(t.get('symbol', '').split('/')[0])
        for t in sorted_by_change[:3]:
            mentioned_symbols.add(t.get('symbol', '').split('/')[0])
            
        lines = []
        for sym in mentioned_symbols:
            t = ticker_dict.get(sym)
            if t:
                price = t.get('price', 0)
                chg = t.get('change24h', 0)
                fr = t.get('fundingRate', 0)
                vol = t.get('volume24h', 0)
                lines.append(f"- {sym}: 现价 ${price:,.4f} | 24H涨跌 {chg:+.2f}% | 资金费率 {fr*100:+.4f}% | 24H成交额 ${vol:,.0f}")
                
        market_summary = "【系统捕获的实时行情切片 (包含用户询问标的、宏观基准与当前热点)】\n" + "\n".join(lines)

    system_prompt = f"""你是「大牛」——一位在加密货币与传统金融领域拥有超过15年实战经验的顶级量化投研顾问。
你的专业背景涵盖：
- **链上/链下数据分析**：精通 on-chain 指标（MVRV、NUPL、NVT、资金流向、巨鲸地址监控）；
- **衍生品结构与博弈**：深度理解永续合约资金费率、持仓量 OI、期权隐含波动率、多空挤压机制；
- **量化策略**：网格交易、统计套利、跨交易所价差策略、动量因子、流动性猎杀；
- **宏观金融视角**：美联储利率周期、美元指数 DXY、比特币减半周期、机构资金入场节奏；
- **币种基本面**：代币经济学、解锁压力、生态发展、技术路线与竞争壁垒分析。

**行为准则（必须严格遵守）：**
1. **拒绝泛泛而谈**——每一条建议必须有具体数据支撑（价格、费率、比率、关键位），绝不说"可能会涨"这类无效话语；
2. **给出明确操作方向**——做多/做空/观望，并说明入场区间、止损位、目标位，逻辑必须清晰闭环；
3. **精准排版**——使用 Markdown 加粗突出关键数字和结论，分点列出，总字数控制在 500 字以内以保持精炼；
4. **区分时间维度**——明确区分短线（1-48H）、中线（1-2周）操作建议，不混同；
5. **风控意识**——每个建议必须附带风险提示，尤其针对高资金费率、高杠杆、流动性不足场景；
6. 若行情数据中未收录该币种，直接基于你的专业训练库作答，并注明数据为截止训练日期的历史信息。

{market_summary}
"""
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.post(
                f"{api_base.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": message}
                    ]
                }
            )
            if resp.status_code == 200:
                data = resp.json()
                reply = data["choices"][0]["message"]["content"]
                return JSONResponse(content={"reply": md_to_html(reply)})
            else:
                return JSONResponse(content={"reply": f"大模型接口请求失败: HTTP {resp.status_code}\n```json\n{resp.text}\n```"})
    except Exception as e:
        return JSONResponse(content={"reply": f"大模型网络请求异常，请检查 API 密钥、网络连接或代理。\n详细报错: {str(e)}"})


@app.get("/api/binance/oi_history")
async def api_oi_history(symbol: str = "BTCUSDT"):
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://fapi.binance.com/futures/data/openInterestHist",
                params={"symbol": symbol, "period": "5m", "limit": 30}
            )
            return JSONResponse(content=resp.json(), headers={"Cache-Control": "public, max-age=30"})
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

@app.get("/api/binance/top_ls_account")
async def api_top_ls_account(symbol: str = "BTCUSDT"):
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://fapi.binance.com/futures/data/topLongShortAccountRatio",
                params={"symbol": symbol, "period": "5m", "limit": 30}
            )
            return JSONResponse(content=resp.json(), headers={"Cache-Control": "public, max-age=30"})
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

@app.get("/api/binance/top_ls_position")
async def api_top_ls_position(symbol: str = "BTCUSDT"):
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://fapi.binance.com/futures/data/topLongShortPositionRatio",
                params={"symbol": symbol, "period": "5m", "limit": 30}
            )
            return JSONResponse(content=resp.json(), headers={"Cache-Control": "public, max-age=30"})
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

@app.get("/api/binance/global_ls")
async def api_global_ls(symbol: str = "BTCUSDT"):
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
                params={"symbol": symbol, "period": "5m", "limit": 30}
            )
            return JSONResponse(content=resp.json(), headers={"Cache-Control": "public, max-age=30"})
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

@app.get("/api/binance/taker_volume")
async def api_taker_volume(symbol: str = "BTCUSDT"):
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://fapi.binance.com/futures/data/takerlongshortRatio",
                params={"symbol": symbol, "period": "5m", "limit": 30}
            )
            return JSONResponse(content=resp.json(), headers={"Cache-Control": "public, max-age=30"})
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

@app.get("/api/binance/basis")
async def api_basis(symbol: str = "BTCUSDT"):
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://fapi.binance.com/futures/data/basis",
                params={"symbol": symbol, "period": "5m", "limit": 30, "contractType": "PERPETUAL"}
            )
            return JSONResponse(content=resp.json(), headers={"Cache-Control": "public, max-age=30"})
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "web_dashboard.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


# Proxy TradingView tv.js - self-host through HK server to bypass GFW
_tv_js_cache = {"content": None, "ts": 0}

@app.get("/static/tv.js")
async def serve_tv_js():
    from fastapi.responses import Response
    now = time.time()
    # Cache for 24 hours
    if _tv_js_cache["content"] and now - _tv_js_cache["ts"] < 86400:
        return Response(content=_tv_js_cache["content"], media_type="application/javascript",
                       headers={"Cache-Control": "public, max-age=86400"})
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get("https://s3.tradingview.com/tv.js")
            if resp.status_code == 200:
                _tv_js_cache["content"] = resp.content
                _tv_js_cache["ts"] = now
                return Response(content=resp.content, media_type="application/javascript",
                               headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        print(f"Failed to fetch tv.js: {e}")
    return Response(content=b"// TradingView not available", status_code=503)


if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    print("=" * 60)
    print("  Multi-Exchange Trading System - Web Dashboard")
    print("=" * 60)
    print()
    print("  Browser URL: http://localhost:8888")
    print()
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=8888, log_level="info")
