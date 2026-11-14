"""Запуск:  python run.py            — живой режим (дашборд + Telegram)
           python run.py once       — один проход
           python run.py backtest BTCUSDT 15m [кол-во_свечей]"""
import json, os, sys, time, collections
from concurrent.futures import ThreadPoolExecutor
import requests
import sources, analysis, news as newsmod

HERE = os.path.dirname(os.path.abspath(__file__))
G, R_, Y, X = "\033[92m", "\033[91m", "\033[93m", "\033[0m"


def fmt(x): return f"{x:,.2f}" if x >= 100 else f"{x:.4f}" if x >= 1 else f"{x:.6f}"


class Bot:
    """Отдельный бот на каждую пару."""
    def __init__(self, p, g):
        self.cfg = {**g, **p}
        self.sym, self.tf, self.htf = p["symbol"].upper(), self.cfg["timeframe"], self.cfg["htf"]
        self.oi = collections.defaultdict(lambda: collections.deque(maxlen=900))
        self.last_side, self.logged, self.win, self.multi = 0, 0, 900, p.get("multi", True)

    def _deriv(self, f):
        raw, now, fund, chg = sources.derivs(self.sym), time.time(), [], []
        for ex, (fr, oi) in raw.items():
            fund.append(fr)
            if oi is None: continue
            h = self.oi[ex]
            old = next((v for t, v in h if t >= now - self.win), None)
            if old and now - h[0][0] > 60 and old > 0:
                chg.append((oi - old) / old)
            h.append((now, oi))
        k = max(1, self.win // (analysis.TFMIN[self.tf] * 60))
        pc = f.close.iloc[-1] / f.close.iloc[-1 - k] - 1
        avg_f = sum(fund) / len(fund)
        oc = sum(chg) / len(chg) if chg else None
        return analysis.deriv_score(avg_f, oc, pc), avg_f, oc

    def step(self, nf):
        df = sources.candles(self.sym, self.tf, 300, mode="all" if self.multi else "first")
        f, bx = analysis.features(df), df.attrs.get("by_ex") or {}
        if time.time() - getattr(self, "_ht", 0) > 300:
            self._hf, self._ht = analysis.features(sources.candles(self.sym, self.htf, 300, mode="all" if self.multi else "first")), time.time()
        hf = self._hf
        d, fr, oc = None, None, None
        try: d, fr, oc = self._deriv(f)
        except Exception: pass
        n, fng, heads = nf.score(sources.base_of(self.sym))
        sig = analysis.build_signal(self.sym, self.tf, f, hf, d, n, self.cfg, btc=btc_ctx())
        sig.update(exch=bx, spread=(max(bx.values()) / min(bx.values()) - 1) * 100 if len(bx) > 1 else 0.0, funding=fr, oi_chg=oc, fng=fng, heads=heads, ts=time.time())
        if abs(sig["score"]) < self.cfg["threshold"] * 0.5: self.last_side = self.logged = 0
        sig["new"] = sig["side"] != 0 and sig["side"] != self.last_side and sig["conf"] >= self.cfg["min_conf"]
        sig["cand"] = sig["side"] != 0 and sig["side"] != self.logged and sig["conf"] >= self.cfg["min_conf"]
        return sig


def render(s):
    side = {1: f"{G}ЛОНГ{X}", -1: f"{R_}ШОРТ{X}", 0: f"{Y}ЖДАТЬ{X}"}[s["side"]]
    out = [f"━━ {s['symbol']} · {s['tf']} ━━ цена {fmt(s['price'])}",
           f"Сигнал: {side}   уверенность {s['conf']}%   скор {s['score']:+.2f}"]
    if s["side"]:
        out += [f"Вход ~{fmt(s['price'])} | СТОП {fmt(s['sl'])} | TP1 {fmt(s['tp1'])} | TP2 {fmt(s['tp2'])}",
                f"Удержание {s['hold']} | объём {s['qty']:.4g} (≈{s['notional']:,.0f}$, плечо {s['leverage']:.1f}x) "
                f"при риске {s['risk_usd']:.0f}$"]
    else:
        lv = s["lv"]
        out.append(f"Сценарии: ЛОНГ стоп {fmt(lv['long'][0])} / TP1 {fmt(lv['long'][1])} | ШОРТ стоп {fmt(lv['short'][0])} / TP1 {fmt(lv['short'][1])}")
    out.append("Факторы: " + " | ".join(f"{k} {v:+.2f}" for k, v in s["parts"].items()))
    extra = []
    if s.get("funding") is not None: extra.append(f"funding {s['funding'] * 100:+.4f}%")
    if s.get("fng") is not None: extra.append(f"Fear&Greed {s['fng']}")
    out.append(" • ".join(extra + s["why"]))
    out += [f"  {'▲' if v > 0 else '▼'} {t[:100]}" for v, t in s["heads"]]
    return "\n".join(out)


def telegram(cfg, s):
    tg = {**cfg.get("telegram", {})}
    tg["token"], tg["chat_id"] = os.environ.get("TG_TOKEN") or tg.get("token"), os.environ.get("TG_CHAT") or tg.get("chat_id")
    if not tg["token"]: return
    txt = (f"{'🟢 ЛОНГ' if s['side'] == 1 else '🔴 ШОРТ'} {s['symbol']} ({s['tf']})\nВход {fmt(s['price'])}\n"
           f"Стоп {fmt(s['sl'])}\nTP1 {fmt(s['tp1'])}\nTP2 {fmt(s['tp2'])}\nУдержание {s['hold']}\n"
           f"Уверенность {s['conf']}%")
    try: requests.post(f"https://api.telegram.org/bot{tg['token']}/sendMessage", json={"chat_id": tg["chat_id"], "text": txt}, timeout=8)
    except Exception: pass


J = os.path.join(HERE, "journal.csv")


def log(s):
    new = not os.path.exists(J)
    with open(J, "a", encoding="utf-8") as f:
        if new: f.write("ts,symbol,tf,side,entry,sl,tp1,tp2,score,conf,mode,valid_until,capped\n")
        t = int(s.get("ts", time.time()))
        f.write(f"{t},{s['symbol']},{s['tf']},{s['side']},{s['entry']},{s['sl']},{s['tp1']},{s['tp2']},{s['score']:.3f},{s['conf']},"
                f"{s['mode']},{t + s['exp_s']},{int(bool(s.get('capped')))}\n")


def judge(r, df, now, fee=0.0008):
    """Проверка одного сигнала по свечам: дошла ли цена до лимитного входа, затем до тейка или до стопа (R — с комиссиями).
    Если в одной свече коснулись и стопа, и тейка — засчитывается СТОП (консервативно)."""
    import pandas as pd
    d, E, SL, T1, T2 = int(r["side"]), r["entry"], r["sl"], r["tp1"], r["tp2"]
    risk = abs(E - SL); rr = abs(T1 - E) / risk; cost = 2 * fee * E / risk
    out = dict(status="waiting", R=None, tp2=False, amb=False, mfe=0.0, mae=0.0, fill_ts=None, exit_ts=None)
    ts = lambda t: int(t.value // 10**9)
    t0 = pd.to_datetime(r["ts"], unit="s")
    if len(df) == 0 or t0 < df.index[0]: out["status"] = "nodata"; return out
    hit_sl = lambda hi, lo: (lo <= SL) if d == 1 else (hi >= SL)
    hit = lambda hi, lo, p: (hi >= p) if d == 1 else (lo <= p)
    filled = r["mode"] != "limit"
    if filled: out["fill_ts"] = int(r["ts"])
    bars = df[df.index > t0]
    for t, hi, lo in zip(bars.index, bars.high, bars.low):
        if not filled:
            if ts(t) > r["valid_until"]: out["status"] = "nofill"; return out
            if (lo <= E) if d == 1 else (hi >= E):
                filled, out["fill_ts"] = True, ts(t)
                if hit_sl(hi, lo): out.update(status="SL", R=-1 - cost, exit_ts=ts(t), amb=True); return out
            continue
        out["mfe"] = max(out["mfe"], ((hi - E) if d == 1 else (E - lo)) / risk)
        out["mae"] = max(out["mae"], ((E - lo) if d == 1 else (hi - E)) / risk)
        if out["R"] is None:
            if hit_sl(hi, lo): out.update(status="SL", R=-1 - cost, exit_ts=ts(t), amb=bool(hit(hi, lo, T1))); return out
            if hit(hi, lo, T1): out.update(status="TP1", R=rr - cost, exit_ts=ts(t))
        elif hit_sl(hi, lo) and not hit(hi, lo, T2): return out
        if out["R"] is not None and hit(hi, lo, T2): out["tp2"] = True; return out
    if not filled: out["status"] = "waiting" if now <= r["valid_until"] else "nofill"
    elif out["R"] is None: out["status"] = "open"
    return out


def evaluate(limit=1000):
    """Все сигналы из journal.csv + что с ними произошло."""
    import pandas as pd
    if not os.path.exists(J): return []
    rows = pd.read_csv(J).tail(limit).to_dict("records")
    keys = sorted({(r["symbol"], r["tf"]) for r in rows})
    def get(k):
        try: return sources.candles(k[0], k[1], 1000, mode="first")
        except Exception: return None
    with ThreadPoolExecutor(8) as p: cache = dict(zip(keys, p.map(get, keys)))
    now, empty = time.time(), dict(status="nodata", R=None, tp2=False, amb=False, mfe=0.0, mae=0.0, fill_ts=None, exit_ts=None)
    return [{**r, **(judge(r, cache[(r["symbol"], r["tf"])], now) if cache[(r["symbol"], r["tf"])] is not None else empty)} for r in rows]


def _wilson(w, n, z=1.96):
    if not n: return 0.0, 0.0
    p, d = w / n, 1 + z * z / n
    c, m = p + z * z / (2 * n), z * (p * (1 - p) / n + z * z / (4 * n * n)) ** .5
    return (c - m) / d, (c + m) / d


def stats(rows):
    closed = [r for r in rows if r["status"] in ("SL", "TP1")]
    cnt = lambda st: sum(r["status"] == st for r in rows)
    fills, nf = len(closed) + cnt("open"), cnt("nofill")
    w, Rs = sum(r["status"] == "TP1" for r in closed), [r["R"] for r in closed]
    lo, hi = _wilson(w, len(closed))
    rr = sum(abs(r["tp1"] - r["entry"]) / abs(r["entry"] - r["sl"]) for r in closed) / len(closed) if closed else 1.5
    cost = sum((abs(r["tp1"] - r["entry"]) / abs(r["entry"] - r["sl"]) - r["R"]) if r["status"] == "TP1" else (-1 - r["R"]) for r in closed) / len(closed) if closed else 0
    be = (1 + cost) / (1 + rr)  # безубыточный винрейт с учётом комиссий
    eq, cur, peak, dd = [], 0.0, 0.0, 0.0
    for r in sorted(closed, key=lambda r: r["exit_ts"]):
        cur += r["R"]; peak = max(peak, cur); dd = max(dd, peak - cur); eq.append(round(cur, 2))
    def grp(f):
        g = [r for r in closed if f(r)]
        return dict(n=len(g), winrate=round(sum(r["status"] == "TP1" for r in g) / len(g) * 100) if g else None,
                    avg_R=round(sum(r["R"] for r in g) / len(g), 2) if g else None)
    gp, gl = sum(x for x in Rs if x > 0), -sum(x for x in Rs if x < 0)
    verdict = ("мало данных: нужно хотя бы 100 закрытых сделок" if len(closed) < 100 else "преимущество вероятно" if lo > be
               else "убыточно" if hi < be else "преимущество не подтверждено")
    return dict(n=len(rows), closed=len(closed), wins=w, open=cnt("open"), waiting=cnt("waiting"), nofill=nf, nodata=cnt("nodata"),
                fill_rate=round(fills / (fills + nf) * 100) if fills + nf else None,
                winrate=round(w / len(closed) * 100) if closed else None, ci=[round(lo * 100), round(hi * 100)], be_winrate=round(be * 100),
                avg_R=round(sum(Rs) / len(Rs), 2) if Rs else None, total_R=round(sum(Rs), 2), profit_factor=round(gp / gl, 2) if gl else None,
                max_dd_R=round(dd, 2), tp2_rate=round(sum(r["tp2"] for r in closed) / w * 100) if w else None, amb=sum(r["amb"] for r in closed),
                equity=eq, by_side={"long": grp(lambda r: r["side"] == 1), "short": grp(lambda r: r["side"] == -1)},
                by_conf={"до 50%": grp(lambda r: r["conf"] < 50), "50–69%": grp(lambda r: 50 <= r["conf"] < 70), "70%+": grp(lambda r: r["conf"] >= 70)},
                verdict=verdict)


def stats_bundle(rows):
    """Общая статистика + отдельная по КАЖДОЙ паре + последние сигналы (для дашборда и приложения)."""
    keep = ["ts", "symbol", "tf", "side", "entry", "sl", "tp1", "tp2", "conf", "status", "R", "tp2", "amb", "capped", "fill_ts", "exit_ts"]
    return {"stats": stats(rows), "per": {s: stats([r for r in rows if r["symbol"] == s]) for s in sorted({r["symbol"] for r in rows})},
            "rows": [{k: r[k] for k in keep} for r in rows[::-1][:400]]}


def publish(res, nf):
    """Снимок для приложения на телефоне: JSON -> gzip -> AES-256-GCM (ключ из пароля PAGES_PASSWORD) -> docs/data.json.
    Страница на GitHub Pages публична, поэтому без пароля данные не читаются."""
    pw = os.environ.get("PAGES_PASSWORD")
    if not pw: return
    import base64, gzip
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    data = {"ts": time.time(), "pairs": [s for s in res if isinstance(s, dict)],
            "mkt": {"btc": _BTC["v"], "fng": nf.fng, "news": "ИИ" if nf.llm else "ключевые слова"}, **stats_bundle(evaluate())}
    raw = gzip.compress(json.dumps(data, default=float).replace("NaN", "null").encode())
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200000).derive(pw.encode())
    b = lambda x: base64.b64encode(x).decode()
    os.makedirs(os.path.join(HERE, "docs"), exist_ok=True)
    json.dump({"v": 1, "salt": b(salt), "iv": b(iv), "ct": b(AESGCM(key).encrypt(iv, raw, None))}, open(os.path.join(HERE, "docs", "data.json"), "w"))
    print("снимок для приложения обновлён")


def report():
    if not os.path.exists(J): return print("журнал сигналов пока пуст")
    rows = evaluate(); s = stats(rows)
    print(f"Сигналов {s['n']}: закрыто {s['closed']} (тейк {s['wins']}, стоп {s['closed'] - s['wins']}), в позиции {s['open']}, "
          f"ждут входа {s['waiting']}, лимитка не исполнилась {s['nofill']}, нет данных {s['nodata']}")
    if s["closed"]:
        print(f"Винрейт {s['winrate']}% (95% интервал {s['ci'][0]}–{s['ci'][1]}%, безубыток {s['be_winrate']}%), средний R {s['avg_R']:+}, итог {s['total_R']:+}R, "
              f"макс. просадка {s['max_dd_R']}R, исполнение лимиток {s['fill_rate']}%")
    print("Вывод:", s["verdict"])
    ST = {"SL": "стоп", "TP1": "тейк", "open": "в позиции", "waiting": "ждёт входа", "nofill": "не исполнен", "nodata": "нет данных"}
    for r in rows[-15:]:
        print(f"  {time.strftime('%d.%m %H:%M', time.gmtime(r['ts']))}Z {r['symbol']:<10} {'ЛОНГ' if r['side'] == 1 else 'ШОРТ'} вход {fmt(r['entry'])} стоп {fmt(r['sl'])} TP1 {fmt(r['tp1'])} -> {ST[r['status']]}"
              + (f" {r['R']:+.2f}R" if r["R"] is not None else ""))


def check():
    for n, fn in sources.EXCHANGES.items():
        try: fn("BTCUSDT", "15m", 5, False); print(f"{n}: OK")
        except Exception as e: print(f"{n}: НЕТ ДОСТУПА ({type(e).__name__})")
    try: print("фьючерсы:", ", ".join(sources.derivs("BTCUSDT")))
    except Exception: print("фьючерсы: НЕТ ДОСТУПА")
    try: requests.get("https://cointelegraph.com/rss", timeout=8).raise_for_status(); print("новости: OK")
    except Exception: print("новости: НЕТ ДОСТУПА")


SF = os.path.join(HERE, "state", "last_sides.json")
OF = os.path.join(HERE, "state", "oi.json")


_BTC = {"t": 0, "v": None}
_BL = __import__("threading").Lock()


def btc_ctx():
    """Состояние BTC (1ч) — альткоины не берём против сильного движения рынка."""
    with _BL:
        if time.time() - _BTC["t"] > 60:
            _BTC["t"] = time.time()
            try: _BTC["v"] = float(analysis.tech_score(analysis.features(sources.candles("BTCUSDT", "1h", 300))).iloc[-1])
            except Exception: pass
    return _BTC["v"]


def commit(bots, res, cfg):
    """Лимит одновременных сигналов (риск не складывается в десяток коррелирующих сделок) + рассылка."""
    n = cfg.get("max_signals", 3)
    act = sorted((s for s in res if isinstance(s, dict) and s.get("side")), key=lambda s: -s["conf"])
    for s in act[n:]:
        s["capped"], s["new"] = True, False
        s["why"].append(f"лимит одновременных сигналов ({n}) — этот пропусти")
    for b, s in zip(bots, res):
        if not isinstance(s, dict): continue
        if s.get("cand"): log(s); b.logged = s["side"]  # в журнал попадают ВСЕ сигналы (и те, что вне лимита) — больше данных для статистики
        if s.get("new"): b.last_side = s["side"]; telegram(cfg, s)


def live_pairs(cfg):
    """Список пар: "auto" — все актуальные USDT-пары с бирж; иначе список из config.json (несуществующие отбрасываются)."""
    if cfg.get("pairs") == "auto":
        top, mv = int(os.environ.get("AUTO_TOP", cfg.get("auto_top", 0))), cfg.get("min_volume_usd", 2e6)
        u = sources.universe(mv, top)
        print(f"Загружено пар с бирж: {len(u)} (оборот от {mv / 1e6:.0f} млн $ в сутки)")
        return [{"symbol": s, "multi": i < cfg.get("multi_top", 30)} for i, (s, _) in enumerate(u)]
    def ok(p):
        try: sources.candles(p["symbol"].upper(), "1h", 5); return True
        except Exception: return False
    with ThreadPoolExecutor(cfg.get("workers", 10)) as pool: oks = list(pool.map(ok, cfg["pairs"]))
    bad = [p["symbol"] for p, o in zip(cfg["pairs"], oks) if not o]
    if len(bad) == len(oks): raise RuntimeError("нет связи с биржами — запусти: python run.py check")
    if bad: print("Пропущены (нет данных на биржах):", ", ".join(bad))
    return [p for p, o in zip(cfg["pairs"], oks) if o]


def cycle(bots, nf, cfg):
    nf.refresh()
    def safe(b):
        try: return b.step(nf)
        except Exception as e: return f"{b.sym}: ошибка — {e}"
    with ThreadPoolExecutor(min(len(bots), cfg.get("workers", 10))) as p: res = list(p.map(safe, bots))
    commit(bots, res, cfg)
    print("\033[2J\033[H" if cfg.get("clear_screen", True) else "", end="")
    print(time.strftime("%H:%M:%S"), "— анализ рынка (не финансовый совет)\n")
    show = [s for s in res if not isinstance(s, dict) or s["side"]] if len(res) > 20 else res
    for s in show:
        print(render(s) if isinstance(s, dict) else f"{R_}{s}{X}", "\n")
    if len(show) < len(res): print(f"Без сигнала: {len(res) - len(show)} пар — все пары смотри в дашборде: python run.py serve")
    return res


def main():
    cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
    a = sys.argv[1:]
    if a and a[0] == "report": return report()
    if a and a[0] == "check": return check()
    if a and a[0] == "serve":
        import server; return server.main(cfg)
    if a and a[0] == "backtest":
        sym, tf = a[1].upper(), a[2] if len(a) > 2 else "15m"
        df = sources.candles(sym, tf, int(a[3]) if len(a) > 3 else 1000)
        r = analysis.backtest(df, cfg["threshold"], cfg["sl_atr"], cfg["rr"], limit_atr=cfg.get("limit_atr", 0.25) if cfg.get("entry_mode", "limit") == "limit" else 0.0)
        print(f"{sym} {tf}: {len(df)} свечей")
        for k, v in r.items(): print(f"  {k}: {v:.2f}" if isinstance(v, float) else f"  {k}: {v}")
        return
    bots = [Bot(p, cfg) for p in live_pairs(cfg)]
    nf = newsmod.NewsFeed(cfg["news_refresh_sec"])
    nf.refresh(force=True)
    if a and a[0] == "once":  # режим GitHub Actions: состояние хранится в репозитории
        cfg["clear_screen"] = False
        try: ls = json.load(open(SF))
        except Exception: ls = {}
        try: oi = json.load(open(OF))
        except Exception: oi = {}
        for b in bots:
            v = ls.get(b.sym) if isinstance(ls.get(b.sym), dict) else {}
            b.last_side, b.logged, b.win = v.get("s", 0), v.get("l", 0), 4000  # между запусками ~1 час: сравниваем OI с прошлым запуском
            for ex, tv in oi.get(b.sym, {}).items(): b.oi[ex].append(tuple(tv))
        res = cycle(bots, nf, cfg)
        os.makedirs(os.path.dirname(SF), exist_ok=True)
        json.dump({b.sym: {"s": b.last_side, "l": b.logged} for b in bots}, open(SF, "w"))
        json.dump({b.sym: {ex: h[-1] for ex, h in b.oi.items() if h} for b in bots}, open(OF, "w"))
        publish(res, nf)
        return
    while True:
        t0 = time.time()
        cycle(bots, nf, cfg)
        time.sleep(max(1, cfg["refresh_sec"] - (time.time() - t0)))


if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: print("\nостановлено")
