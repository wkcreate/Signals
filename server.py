"""Локальный дашборд: python run.py serve  ->  http://localhost:8000"""
import json, os, threading, time, http.server
from concurrent.futures import ThreadPoolExecutor
import re, urllib.parse
import run, sources, news as newsmod

_CC = {}
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".json": "application/json", ".webmanifest": "application/manifest+json", ".png": "image/png"}
_ST = {"t": 0, "v": {}}


def stats_cached():
    if time.time() - _ST["t"] > 600:
        _ST["t"] = time.time()
        try:
            rows = run.evaluate()
            _ST["v"] = run.stats_bundle(rows)
        except Exception: pass
    return _ST["v"]


def candles_json(sym, tf):
    """Свечи для графика (агрегат по биржам), кэш 5 с."""
    t, v = _CC.get((sym, tf), (0, None))
    if time.time() - t < 5: return v
    df = sources.candles(sym, tf, 300)
    v = [{"time": int(i.value // 10**9), "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume}
         for i, r in df.iterrows()]
    _CC[(sym, tf)] = (time.time(), v)
    return v

STATE = {"pairs": [], "ts": 0}
HERE = os.path.dirname(os.path.abspath(__file__))


def loop(cfg):
    bots, nf, t0 = {}, newsmod.NewsFeed(cfg["news_refresh_sec"]), 0
    def one(b):
        try: return b.step(nf)
        except Exception as e: return {"symbol": b.sym, "error": str(e)}
    with ThreadPoolExecutor(cfg.get("workers", 10)) as pool:
        while True:
            if time.time() - t0 > 6 * 3600:  # список пар обновляется с бирж раз в 6 часов
                try:
                    keep = bots
                    bots = {p["symbol"].upper(): keep.get(p["symbol"].upper()) or run.Bot(p, cfg) for p in run.live_pairs(cfg)}
                    t0 = time.time()
                except Exception as e:
                    print("список пар:", e)
                    if not bots: time.sleep(10); continue
            nf.refresh(); bl = list(bots.values())
            res = list(pool.map(one, bl)); run.commit(bl, res, cfg)
            STATE.update(pairs=res, ts=time.time(), mkt={"btc": run._BTC["v"], "fng": nf.fng, "news": "ИИ" if nf.llm else "ключевые слова"})
            time.sleep(cfg["refresh_sec"])


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        code = 200
        if self.path.startswith("/api/candles"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            sym, tf = q.get("symbol", [""])[0].upper(), q.get("tf", ["15m"])[0]
            try:
                if not re.fullmatch(r"[A-Z0-9]{3,20}", sym) or tf not in sources.BYBIT_TF: raise ValueError("bad request")
                body = json.dumps(candles_json(sym, tf)).encode()
            except Exception as e:
                body, code = json.dumps({"error": str(e)}).encode(), 502
            ct = "application/json"
        elif self.path.startswith("/api/stats"):
            body, ct = json.dumps(stats_cached(), default=float).replace("NaN", "null").encode(), "application/json"
        elif self.path.startswith("/api/state"):
            body, ct = json.dumps(STATE, default=float).replace("NaN", "null").encode(), "application/json"
        else:
            root = os.path.join(HERE, "docs")
            fp = os.path.normpath(os.path.join(root, self.path.split("?")[0].lstrip("/") or "index.html"))
            if not fp.startswith(root) or not os.path.isfile(fp): body, code, ct = b"not found", 404, "text/plain"
            else: body, ct = open(fp, "rb").read(), MIME.get(os.path.splitext(fp)[1], "application/octet-stream")
        self.send_response(code); self.send_header("Content-Type", ct); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass


def main(cfg):
    threading.Thread(target=loop, args=(cfg,), daemon=True).start()
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"Дашборд: http://localhost:8000  (Ctrl+C — стоп)")
    http.server.ThreadingHTTPServer((host, 8000), H).serve_forever()
