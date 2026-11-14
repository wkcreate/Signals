"""Walk-forward оптимизация: параметры подбираются на прошлом, проверяются на НЕВИДИМОМ будущем.
python optimize.py BTCUSDT ETHUSDT 15m"""
import itertools, sys, numpy as np, analysis, sources

GRID = list(itertools.product([.25, .3, .35, .4, .5], [1, 1.5, 2, 2.5], [1, 1.5, 2, 3], [24, 48]))


def walk(df, folds=5):
    seg, ins, oos = len(df) // (folds + 1), [], []
    for k in range(1, folds + 1):
        tr, te = df.iloc[:k * seg], df.iloc[k * seg - 210:(k + 1) * seg]
        best, bs = None, -9
        for thr, sl, rr, mb in GRID:
            R = analysis.backtest(tr, thr, sl, rr, mb, raw=True)
            if len(R) >= 20 and R.mean() > bs: best, bs = (thr, sl, rr, mb), R.mean()
        if best: ins.append(bs); oos += list(analysis.backtest(te, *best, raw=True))
    return ins, np.array(oos)


def main(args):
    tf = args[-1] if args[-1][-1] in "mhd" else "15m"
    for sym in [a.upper() for a in args if a.upper() != tf.upper()]:
        ins, oos = walk(sources.candles(sym, tf, 1000))
        print(f"{sym} {tf}: комбинаций {len(GRID)} × 5 окон | на обучении avg {np.mean(ins):+.2f}R | "
              f"на НЕВИДИМЫХ данных: сделок {len(oos)}, avg {oos.mean() if len(oos) else 0:+.2f}R, итог {oos.sum():+.1f}R")
        print("  вывод:", "есть кандидат на преимущество" if len(oos) > 30 and oos.mean() > 0.05 else "преимущество НЕ подтверждено")


if __name__ == "__main__": main(sys.argv[1:] or ["BTCUSDT"])
