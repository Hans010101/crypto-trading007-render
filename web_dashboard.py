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


def _parse_ls_ai_response(raw_text: str):
    """Parse LLM JSON response with enhanced fallback."""
    text = raw_text.strip()
    # Strip markdown code block wrappers
    text = re.sub(r'^```(?:json)?\s*\n?', '', text)
    text = re.sub(r'\n?\s*```\s*$', '', text)
    # Try direct parse
    try:
        data = json.loads(text)
        if 'direction' in data:
            return data
    except (json.JSONDecodeError, TypeError):
        pass
    # Extract first JSON object from surrounding text
    match = re.search(r'\{[\s\S]*\}', text)
    if match:
        try:
            data = json.loads(match.group())
            if 'direction' in data:
                return data
        except (json.JSONDecodeError, TypeError):
            pass
    return None


LS_AI_ANALYSIS_PROMPT = """你是一位专业的加密货币合约交易分析师。根据以下多空博弈数据，输出结构化的深度分析。

## 输入数据说明
- OI (持仓量): openInterest, oiChange (变化百分比), oiChangeValue (变化金额)
- 大户账户多空比: topTraderAccountRatio (>1偏多, <1偏空)
- 大户持仓多空比: topTraderPositionRatio (>1偏多, <1偏空)
- 全市场多空人数比: globalLongShortRatio (>1偏多, <1偏空)
- 主动买卖比: takerBuySellRatio (>1买方主导, <1卖方主导)
- 基差率: basisRate (正=升水看涨, 负=贴水看跌)

## 判断规则
对以下 6 项逐一判断多/空：
1. OI变化: 增仓=多, 减仓=空
2. 大户账户比: >1=多, <1=空
3. 大户持仓比: >1=多, <1=空
4. 全市场人数比: >1=多, <1=空（注意：散户极度偏多时反而是风险信号）
5. 主动买卖比: >1=多, <1=空
6. 基差率: >0=多, <0=空

看多数量：
- 5-6项看多 = "偏多"
- 3-4项看多 = 根据权重综合判断，偏多则"偏多"，偏空则"偏空"，均衡则"震荡"
- 0-2项看多 = "偏空"

## 输出要求
请严格按以下 JSON 格式输出，不要输出任何其他内容：

```json
{
  "direction": "偏多" | "偏空" | "震荡",
  "bullCount": 4,
  "totalCount": 6,
  "conflictNote": "持仓量偏空、基差偏空，注意分歧",
  "analysis": {
    "funding": {
      "title": "资金面",
      "content": "OI 减仓 -1.1%（$1.89M），部分资金撤离。主动买卖比 1.770，买方占明显优势，多头在主动进攻。资金撤离但买方仍在，可能是空头平仓，下跌空间有限。"
    },
    "institution": {
      "title": "机构面",
      "content": "大户账户比 1.507（多60.1%），持仓比 1.070（多51.7%），方向一致偏多，机构整体看好后市。"
    },
    "sentiment": {
      "title": "情绪面",
      "content": "散户人数比 1.789（多64.1%），散户极度偏多，需警惕反向收割风险。基差 -0.1300% 贴水，合约折价，市场情绪偏悲观。散户偏多但基差贴水，情绪面出现背离，多头需谨慎。"
    }
  },
  "anomalies": [
    "持仓量短时骤降 17.8%，资金撤离信号明显",
    "大户账户看空但持仓看多，可能在底部建仓",
    "散户极度偏多 (64.1%)，警惕反向收割"
  ],
  "strategy": {
    "summary": "多方略占优势，但持仓量偏空、基差偏空，存在分歧。方向偏多但分歧尚存，建议轻仓试探或等待信号进一步确认。",
    "action": "轻仓试多",
    "position": "仓位 ≤15%",
    "stoploss": "严格止损"
  }
}
```

## 分析要求
1. 资金面：综合 OI变化 和 主动买卖比，分析资金流向和买卖力量对比，给出结论性判断
2. 机构面：综合 大户账户比 和 大户持仓比，分析机构态度，注意账户比和持仓比方向不一致的情况
3. 情绪面：综合 全市场人数比 和 基差率，分析散户情绪和市场预期，注意背离信号
4. 异常信号：找出数据中的矛盾或极端值（如OI骤降>10%、散户比>1.5、大户账户与持仓方向相反等）
5. 策略建议：根据综合判断给出操作方向、仓位建议、风控提示
6. content 中要包含具体数据数值，不要只说定性结论
7. 如果某个维度的两个指标方向矛盾，要明确指出并分析原因

重要：只输出 JSON，不要输出任何其他文字、解释或 markdown 代码块标记。"""


@app.get("/api/ai/ls_analysis")
async def api_ai_ls_analysis(symbol: str):
    """L/S analysis: try LLM structured JSON, fallback to template."""
    base = "https://fapi.binance.com/futures/data"
    urls = {
        "oi": f"{base}/openInterestHist?symbol={symbol}&period=5m&limit=30",
        "topAcc": f"{base}/topLongShortAccountRatio?symbol={symbol}&period=5m&limit=30",
        "topPos": f"{base}/topLongShortPositionRatio?symbol={symbol}&period=5m&limit=30",
        "globalLS": f"{base}/globalLongShortAccountRatio?symbol={symbol}&period=5m&limit=30",
        "taker": f"{base}/takerlongshortRatio?symbol={symbol}&period=5m&limit=30",
        "basis": f"{base}/basis?pair={symbol}&period=5m&limit=30&contractType=PERPETUAL",
    }
    d = {}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            for k, url in urls.items():
                r = await client.get(url)
                d[k] = r.json() if r.status_code == 200 else []
    except Exception:
        return JSONResponse(content={"analysis": "数据获取失败，请稍后重试。", "structured": None})

    def safe_f(arr, key, idx=-1):
        try: return float(arr[idx][key])
        except: return 0

    oi_last = safe_f(d["oi"], "sumOpenInterestValue")
    oi_first = safe_f(d["oi"], "sumOpenInterestValue", 0)
    oi_chg = ((oi_last / oi_first - 1) * 100) if oi_first else 0
    oi_chg_val = oi_last - oi_first

    top_acc_r = safe_f(d["topAcc"], "longShortRatio")
    top_acc_l = safe_f(d["topAcc"], "longAccount") * 100
    top_pos_r = safe_f(d["topPos"], "longShortRatio")
    top_pos_l = safe_f(d["topPos"], "longAccount") * 100
    global_r = safe_f(d["globalLS"], "longShortRatio")
    global_l = safe_f(d["globalLS"], "longAccount") * 100
    taker_r = safe_f(d["taker"], "buySellRatio")
    basis_rate = safe_f(d["basis"], "basisRate") * 100

    def fmt_v(v):
        if abs(v) >= 1e9: return f"${v/1e9:.2f}B"
        if abs(v) >= 1e6: return f"${v/1e6:.2f}M"
        return f"${v:,.0f}"

    # Build data summary for LLM
    data_summary = f"""## {symbol} 多空博弈数据
- 持仓量(OI): {fmt_v(oi_last)}，变化 {oi_chg:+.2f}%（{fmt_v(oi_chg_val)}）
- 大户账户多空比: {top_acc_r:.3f}（多头账户占比 {top_acc_l:.1f}%）
- 大户持仓多空比: {top_pos_r:.3f}（多头持仓占比 {top_pos_l:.1f}%）
- 全市场多空人数比: {global_r:.3f}（多头人数占比 {global_l:.1f}%）
- 主动买卖比: {taker_r:.3f}
- 基差率: {basis_rate:.4f}%"""

    # Try LLM call
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    api_base = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")
    model = os.environ.get("LLM_MODEL", "gpt-3.5-turbo")

    if os.environ.get("DEEPSEEK_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
        api_base = "https://api.deepseek.com/v1"
        model = "deepseek-chat"

    structured = None
    if api_key:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{api_base.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": LS_AI_ANALYSIS_PROMPT},
                            {"role": "user", "content": data_summary}
                        ]
                    }
                )
                if resp.status_code == 200:
                    raw = resp.json()["choices"][0]["message"]["content"]
                    structured = _parse_ls_ai_response(raw)
        except Exception:
            pass

    # Always build template fallback HTML
    bull_signals = sum([top_acc_r > 1, top_pos_r > 1, global_r > 1, taker_r > 1, oi_chg > 0, basis_rate > 0])
    if bull_signals >= 4:
        bias, bias_color, bias_icon = "偏多", "var(--gain)", "🟢"
    elif bull_signals <= 2:
        bias, bias_color, bias_icon = "偏空", "var(--loss)", "🔴"
    else:
        bias, bias_color, bias_icon = "多空均衡", "var(--text-muted)", "🟡"

    def clr(v, threshold=0):
        return "var(--gain)" if v > threshold else ("var(--loss)" if v < threshold else "var(--text-muted)")

    def ratio_desc(r):
        if r > 1.5: return "多头占绝对优势"
        if r > 1.1: return "多头略占上风"
        if r > 0.9: return "多空势均力敌"
        if r > 0.7: return "空头略占上风"
        return "空头占绝对优势"

    oi_desc = "增仓" if oi_chg > 0 else "减仓"
    taker_desc = "主动买入力量较强" if taker_r > 1 else "主动卖出力量较强"
    basis_desc = "正溢价（看涨预期）" if basis_rate > 0 else "负溢价（看跌预期）"

    anomalies = []
    if abs(oi_chg) > 3:
        anomalies.append(f"持仓量短时{'暴增' if oi_chg > 0 else '骤降'} {abs(oi_chg):.1f}%，{'新资金入场' if oi_chg > 0 else '资金撤离'}信号明显")
    if top_acc_r > 1.5 or top_acc_r < 0.67:
        anomalies.append(f"大户账户多空比 {top_acc_r:.3f} 处于极端值，{'大户集中做多' if top_acc_r > 1.5 else '大户集中做空'}")
    if abs(taker_r - 1) > 0.15:
        anomalies.append(f"主动买卖比 {taker_r:.3f} 偏离均衡，{'买方aggressively入场' if taker_r > 1 else '卖方大幅抛售'}")
    if abs(basis_rate) > 0.05:
        anomalies.append(f"基差率 {basis_rate:.4f}% 显著偏离，市场存在{'看涨' if basis_rate > 0 else '看跌'}情绪溢价")
    if top_acc_r > 1 and top_pos_r < 1:
        anomalies.append("大户账户看多但持仓看空，存在对冲或诱多迹象")
    elif top_acc_r < 1 and top_pos_r > 1:
        anomalies.append("大户账户看空但持仓看多，可能在底部建仓")
    anomaly_html = "".join(f"<div style='margin:3px 0;'>⚡ {a}</div>" for a in anomalies) if anomalies else "<div style='color:var(--text-muted);'>当前无明显异常信号</div>"

    html = f"""
    <div style="margin-bottom:16px;">
      <div style="font-size:18px;font-weight:700;color:{bias_color};margin-bottom:8px;">{bias_icon} 综合研判：{bias} ({bull_signals}/6 项看多)</div>
    </div>
    <div style="margin-bottom:14px;">
      <strong style="color:var(--text-primary);">📊 持仓量分析</strong><br>
      <div style="color:var(--text-secondary);margin-top:4px;">
        持仓总价值 <span style="color:var(--text-primary);font-weight:600;">{fmt_v(oi_last)}</span>，
        2.5h 内<span style="color:{clr(oi_chg)};font-weight:600;">{oi_desc} {oi_chg:+.2f}%</span>。
        {'资金持续流入，市场参与度增加，利于趋势延续。' if oi_chg > 0 else '资金退出，市场分歧加大，短线波动可能加剧。'}
      </div>
    </div>
    <div style="margin-bottom:14px;">
      <strong style="color:var(--text-primary);">🐋 大户动向</strong><br>
      <div style="color:var(--text-secondary);margin-top:4px;">
        账户多空比 <span style="color:{clr(top_acc_r, 1)};font-weight:600;">{top_acc_r:.3f}</span>（多 {top_acc_l:.1f}%），{ratio_desc(top_acc_r)}。<br>
        持仓多空比 <span style="color:{clr(top_pos_r, 1)};font-weight:600;">{top_pos_r:.3f}</span>（多 {top_pos_l:.1f}%），{ratio_desc(top_pos_r)}。
      </div>
    </div>
    <div style="margin-bottom:14px;">
      <strong style="color:var(--text-primary);">👥 散户情绪</strong><br>
      <div style="color:var(--text-secondary);margin-top:4px;">
        全市场多空人数比 <span style="color:{clr(global_r, 1)};font-weight:600;">{global_r:.3f}</span>（多 {global_l:.1f}%），{ratio_desc(global_r)}。
        {'散户偏多，需警惕反向收割风险。' if global_r > 1.2 else ('散户偏空，逆向指标显示可能见底。' if global_r < 0.8 else '散户情绪中性。')}
      </div>
    </div>
    <div style="margin-bottom:14px;">
      <strong style="color:var(--text-primary);">💥 主动买卖</strong><br>
      <div style="color:var(--text-secondary);margin-top:4px;">
        买卖比 <span style="color:{clr(taker_r, 1)};font-weight:600;">{taker_r:.3f}</span>，{taker_desc}。
        {'多头在主动进攻，短线偏强。' if taker_r > 1.05 else ('空头在主动抛售，短线偏弱。' if taker_r < 0.95 else '买卖力量基本均衡。')}
      </div>
    </div>
    <div style="margin-bottom:14px;">
      <strong style="color:var(--text-primary);">📐 基差信号</strong><br>
      <div style="color:var(--text-secondary);margin-top:4px;">
        基差率 <span style="color:{clr(basis_rate)};font-weight:600;">{basis_rate:.4f}%</span>，{basis_desc}。
        {'期货溢价较高，市场看涨情绪浓厚，但需防高位回调。' if basis_rate > 0.02 else ('期货折价，空头主导，但深度折价往往暗示超卖反弹机会。' if basis_rate < -0.02 else '基差处于正常范围，期现价格基本一致。')}
      </div>
    </div>
    <div style="margin-bottom:14px;">
      <strong style="color:var(--text-primary);">⚠️ 异常信号检测</strong><br>
      <div style="color:var(--text-secondary);margin-top:4px;">{anomaly_html}</div>
    </div>
    <div style="padding:10px;background:var(--bg-hover);border-radius:8px;border:1px solid var(--border-color);">
      <strong style="color:var(--text-primary);">📌 操作建议</strong><br>
      <div style="color:var(--text-secondary);margin-top:4px;">
        {'大户与散户均偏多，持仓增加，建议顺势做多，回调至支撑位可加仓。止损设在近期低点下方。' if bull_signals >= 5 else ('多项指标偏空，建议谨慎持仓或轻仓做空。关注持仓量变化，若出现放量反弹应及时止损。' if bull_signals <= 1 else '多空信号交织，建议观望为主。若选择入场，控制仓位在20%以内，严格设置止损。')}
      </div>
    </div>
    """
    return JSONResponse(content={"analysis": html, "structured": structured})


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
                params={"pair": symbol, "period": "5m", "limit": 30, "contractType": "PERPETUAL"}
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
