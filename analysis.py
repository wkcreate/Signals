"""Индикаторы, скоринг, генерация сигнала (вход / стоп / тейки / размер) и бэктест."""
import numpy as np, pandas as pd

TFMIN = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240, "1d": 1440}


def ema(s, n): return s.ewm(span=n, adjust=False).mean()


def rsi(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def atr(df, n=14):
    pc = df.close.shift()
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx(df, n=14):
    up, dn = df.high.diff(), -df.low.diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    a = atr(df, n)
    pdi = 100 * plus.ewm(alpha=1 / n, adjust=False).mean() / a
    mdi = 100 * minus.ewm(alpha=1 / n, adjust=False).mean() / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def features(df):
    f, c = df.copy(), df.close
    f["ema20"], f["ema50"], f["ema200"] = ema(c, 20), ema(c, 50), ema(c, 200)
    m = ema(c, 12) - ema(c, 26)
    f["macd_h"] = m - ema(m, 9)
    f["rsi"], f["atr"], f["adx"] = rsi(c), atr(f), adx(f)
    mid, sd = c.rolling(20).mean(), c.rolling(20).std()
    f["bbp"] = (c - (mid - 2 * sd)) / (4 * sd).replace(0, np.nan)
    return f


def tech_score(f):
    """Технический скор [-1..1]. В тренде (ADX>=22) — по тренду, во флэте — от границ (контртренд)."""
    sg = np.sign
    trend = 0.5 * sg(f.ema20 - f.ema50) + 0.3 * sg(f.ema50 - f.ema200) + 0.2 * sg(f.close - f.ema20)
    mom = 0.6 * np.clip(f.macd_h / f.atr * 3, -1, 1) + 0.4 * np.clip((f.rsi - 50) / 25, -1, 1)
    mr = -0.6 * np.clip((f.bbp - 0.5) * 2.4, -1, 1) - 0.4 * np.clip((f.rsi - 50) / 20, -1, 1)
    t = f.adx >= 22
    s = np.where(t, .5, .15) * trend + np.where(t, .4, .2) * mom + np.where(t, .1, .65) * mr
    return pd.Series(s, index=f.index).clip(-1, 1)


def deriv_score(funding, oi_chg, price_chg):
    """Фандинг (контрарно: перегретые лонги = минус) + подтверждение движения ростом OI."""
    s = -np.clip(funding / 0.0005, -1, 1) * 0.5
    if oi_chg is not None and price_chg:
        s += np.clip(oi_chg / 0.02, -1, 1) * np.sign(price_chg) * 0.5
    return float(np.clip(s, -1, 1))


def build_signal(sym, tf, f, hf, deriv=None, news=None, cfg=None, btc=None):
    cfg = cfg or {}
    thr, sl_k, rr = cfg.get("threshold", 0.35), cfg.get("sl_atr", 1.5), cfg.get("rr", 1.5)
    risk_pct, deposit = cfg.get("risk_pct", 1.0), cfg.get("deposit", 1000)
    last, a = f.iloc[-1], f.atr.iloc[-1]
    parts = {"тех": (tech_score(f).iloc[-1], .55), "старший ТФ": (tech_score(hf).iloc[-1], .15)}
    if deriv is not None: parts["фьючерсы"] = (deriv, .15)
    if news is not None: parts["новости"] = (news, .15)
    score = sum(v * w for v, w in parts.values()) / sum(w for _, w in parts.values())
    side = 1 if score >= thr else -1 if score <= -thr else 0
    why = []
    htf = parts["старший ТФ"][0]
    if side and htf * side < -0.5:
        why.append("сигнал против старшего таймфрейма — вход отменён")
        side = 0
    px = float(last.close)
    if side and btc is not None and sym != "BTCUSDT" and cfg.get("btc_filter", True) and btc * side < -0.5:
        why.append("BTC сильно против направления — вход отменён"); side = 0
    if side and a / px * 100 < cfg.get("min_atr_pct", 0.15):
        why.append(f"волатильность {a / px * 100:.2f}% слишком мала — комиссии съедят прибыль"); side = 0
    sig = dict(symbol=sym, tf=tf, side=side, score=float(score), conf=int(min(100, abs(score) / 0.8 * 100)),
               price=px, parts={k: float(v) for k, (v, _) in parts.items()}, why=why)
    r0 = sl_k * a
    k = cfg.get("limit_atr", 0.25) if cfg.get("entry_mode", "limit") == "limit" else 0.0
    el, es = px - k * a, px + k * a  # лимитный вход: лонг ниже рынка, шорт выше (ждём откат)
    sig["lv"] = {"long": [el - r0, el + r0 * rr, el + r0 * rr * 2], "short": [es + r0, es - r0 * rr, es - r0 * rr * 2]}
    sig["en"] = {"long": el, "short": es}
    sig["ind"] = dict(rsi=float(last.rsi), adx=float(last.adx), atr_pct=float(a / px * 100), bbp=float(last.bbp),
                      macd=float(last.macd_h / a), trend=int(np.sign(last.ema20 - last.ema50) + np.sign(last.ema50 - last.ema200)))
    why.append(f"режим: {'тренд' if last.adx >= 22 else 'флэт'} (ADX {last.adx:.0f}), RSI {last.rsi:.0f}")
    if side:
        risk, ent = sl_k * a, (el if side > 0 else es)
        sig.update(entry=ent, mode="limit" if k else "market", sl=ent - side * risk, tp1=ent + side * risk * rr, tp2=ent + side * risk * rr * 2, rr=rr)
        n = max(1.0, (rr * risk / (0.8 * a)) ** 2)  # грубая оценка по случайному блужданию
        mins = n * TFMIN.get(tf, 15)
        sig["hold"] = f"~{mins / 60:.1f} ч" if mins >= 90 else f"~{mins:.0f} мин"
        sig["expire"] = f"{mins * 3 / 60:.0f} ч" if mins * 3 >= 90 else f"{mins * 3:.0f} мин"
        sig["exp_s"] = int(mins * 3 * 60)
        qty = deposit * risk_pct / 100 / risk
        sig.update(qty=qty, notional=qty * ent, leverage=qty * ent / deposit, risk_usd=deposit * risk_pct / 100)
    return sig


def backtest(df, thr=0.35, sl_atr=1.5, rr=1.5, max_bars=48, fee=0.0008, raw=False, min_atr=0.0015, limit_atr=0.25, fill_bars=8):
    """Тест ТЕХНИЧЕСКОЙ части на истории, лимитный вход как в live (limit_atr=0 — вход по рынку). SL проверяется раньше TP."""
    f = features(df)
    s, atr_, h, l, c = tech_score(f).values, f.atr.values, f.high.values, f.low.values, f.close.values
    n, i, R = len(f), 210, []
    while i < n - 1:
        if abs(s[i]) < thr or np.isnan(atr_[i]) or atr_[i] / c[i] < min_atr:
            i += 1; continue
        d, risk = (1 if s[i] > 0 else -1), sl_atr * atr_[i]
        e = c[i] - d * limit_atr * atr_[i]
        sl, tp = e - d * risk, e + d * risk * rr
        r, j = None, i
        if limit_atr > 0:
            jf = next((q for q in range(i + 1, min(i + 1 + fill_bars, n)) if ((l[q] <= e) if d == 1 else (h[q] >= e))), None)
            if jf is None:
                i += fill_bars; continue  # лимитка не исполнилась
            j = jf
            if (l[jf] <= sl) if d == 1 else (h[jf] >= sl): r = -1.0
        start = j + 1
        if r is None:
            j = start
            while j < min(start + max_bars, n):
                if (l[j] <= sl) if d == 1 else (h[j] >= sl): r = -1.0; break
                if (h[j] >= tp) if d == 1 else (l[j] <= tp): r = rr; break
                j += 1
            if r is None:
                j = min(start + max_bars, n) - 1; r = d * (c[j] - e) / risk
        R.append(r - 2 * fee * e / risk)
        i = j + 1
    R = np.array(R)
    if raw: return R
    if not len(R): return dict(trades=0)
    eq = np.cumsum(R)
    gp, gl = R[R > 0].sum(), -R[R < 0].sum()
    return dict(trades=len(R), winrate=float((R > 0).mean() * 100), avg_R=float(R.mean()), total_R=float(R.sum()),
                profit_factor=float(gp / gl) if gl else float("inf"),
                max_dd_R=float((np.maximum.accumulate(eq) - eq).max()),
                buy_hold_pct=float((c[-1] / c[210] - 1) * 100))
