"""Рыночные данные из публичных API нескольких бирж (ключи не нужны).
Цены агрегируются медианой по биржам -> система не зависит от одной площадки."""
import re, threading, time
import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor

S = requests.Session()
S.headers["User-Agent"] = "cryptosignals/1.0"
COLS = ["open", "high", "low", "close", "volume"]
BYBIT_TF = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "2h": "120", "4h": "240", "1d": "D"}
OKX_TF = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H", "2h": "2H", "4h": "4H", "1d": "1D"}


def _j(url, **params):
    r = S.get(url, params=params, timeout=8)
    r.raise_for_status()
    return r.json()


def base_of(sym):
    return sym[:-4] if sym.endswith("USDT") else sym


def _df(rows):
    idx = pd.to_datetime([int(r[0]) for r in rows], unit="ms")
    df = pd.DataFrame([[float(r[i]) for i in (1, 2, 3, 4, 5)] for r in rows], index=idx, columns=COLS)
    return df.sort_index()


def binance(sym, tf, limit, fut):
    url = "https://fapi.binance.com/fapi/v1/klines" if fut else "https://api.binance.com/api/v3/klines"
    return _df(_j(url, symbol=sym, interval=tf, limit=limit))


def bybit(sym, tf, limit, fut):
    d = _j("https://api.bybit.com/v5/market/kline", category="linear" if fut else "spot",
           symbol=sym, interval=BYBIT_TF[tf], limit=limit)
    return _df(d["result"]["list"])


def okx(sym, tf, limit, fut):
    inst = f"{base_of(sym)}-USDT" + ("-SWAP" if fut else "")
    d = _j("https://www.okx.com/api/v5/market/candles", instId=inst, bar=OKX_TF[tf], limit=min(limit, 300))
    return _df(d["data"])


EXCHANGES = {"binance": binance, "bybit": bybit, "okx": okx}


def candles(sym, tf, limit=300, fut=False, exchanges=None, mode="all"):
    """Свечи, агрегированные по всем доступным биржам (медиана OHLC)."""
    names = exchanges or (["bybit", "binance", "okx"] if mode == "first" else list(EXCHANGES))
    if mode == "first":  # для сотен пар: один запрос вместо трёх (первая ответившая биржа)
        for n in names:
            try:
                d = EXCHANGES[n](sym, tf, limit, fut)
                if len(d):
                    d.attrs["sources"], d.attrs["by_ex"] = 1, {n: float(d.close.iloc[-1])}
                    return d
            except Exception:
                pass
        raise RuntimeError(f"{sym}: ни одна биржа не ответила")

    def one(n):
        try:
            return EXCHANGES[n](sym, tf, limit, fut)
        except Exception:
            return None

    with ThreadPoolExecutor(len(names)) as p:
        got = {k: v for k, v in zip(names, p.map(one, names)) if v is not None and len(v)}
    res = list(got.values())
    if not res:
        raise RuntimeError(f"{sym}: ни одна биржа не ответила (проверь интернет/VPN/символ)")
    g = pd.concat(res).groupby(level=0)
    out = pd.DataFrame({"open": g.open.median(), "high": g.high.median(), "low": g.low.median(),
                        "close": g.close.median(), "volume": g.volume.sum()}).sort_index().tail(limit)
    out.attrs["sources"] = len(res)
    out.attrs["by_ex"] = {k: float(v.close.iloc[-1]) for k, v in got.items()}
    return out


_DC = {"t": 0, "d": {}}
_DL = threading.Lock()


def _bulk():
    """Funding и OI сразу по всем контрактам: Binance (funding) и Bybit (funding + OI), кэш 15 с."""
    with _DL:
        if time.time() - _DC["t"] < 15 and _DC["d"]:
            return _DC["d"]
        d = {"binance": {}, "bybit": {}}
        try:
            for r in _j("https://fapi.binance.com/fapi/v1/premiumIndex"):
                d["binance"][r["symbol"]] = (float(r["lastFundingRate"]), None)
        except Exception:
            pass
        try:
            for r in _j("https://api.bybit.com/v5/market/tickers", category="linear")["result"]["list"]:
                d["bybit"][r["symbol"]] = (float(r["fundingRate"] or 0), float(r["openInterest"] or 0))
        except Exception:
            pass
        _DC.update(t=time.time(), d=d)
        return d


def derivs(sym):
    """Фьючерсы: {биржа: (funding, open_interest | None)}; для PEPE/SHIB/BONK ищет контракт 1000X."""
    out = {}
    for ex, m in _bulk().items():
        for k in (sym, "1000" + sym):
            if k in m:
                out[ex] = m[k]
                break
    if not out:
        raise RuntimeError("нет данных по деривативам")
    return out


STABLE = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "PYUSD", "USD1", "EUR", "EURI", "AEUR", "USDS", "UST", "XUSD", "RLUSD", "BFUSD"}


def universe(min_vol=2e6, top=0):
    """Все актуальные USDT-пары: объединение Binance/Bybit/OKX, по суточному обороту в $ (стейблы и плечевые токены убраны)."""
    vol = {}

    def put(sym, v):
        if sym.endswith("USDT") and v:
            vol[sym] = max(vol.get(sym, 0.0), float(v))
    try:
        for r in _j("https://api.binance.com/api/v3/ticker/24hr"): put(r["symbol"], r["quoteVolume"])
    except Exception: pass
    try:
        for r in _j("https://api.bybit.com/v5/market/tickers", category="spot")["result"]["list"]: put(r["symbol"], r["turnover24h"])
    except Exception: pass
    try:
        for r in _j("https://www.okx.com/api/v5/market/tickers", instType="SPOT")["data"]:
            if r["instId"].endswith("-USDT"): put(r["instId"].replace("-", ""), r["volCcy24h"])
    except Exception: pass
    bad = re.compile(r"(\d+[LS]|BULL|BEAR|DOWN)$")
    out = sorted(((s, v) for s, v in vol.items() if v >= min_vol and base_of(s) not in STABLE and base_of(s).isascii()
                  and not bad.search(base_of(s))), key=lambda x: -x[1])
    if not out: raise RuntimeError("не удалось получить список пар (нет связи с биржами)")
    return out[:top] if top else out
