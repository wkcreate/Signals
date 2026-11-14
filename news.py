"""Новости (RSS) + Fear&Greed -> оценка настроений в диапазоне [-1, 1]."""
import os, json, re, math, time, threading, requests
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

FEEDS = ["https://www.coindesk.com/arc/outboundfeeds/rss/", "https://cointelegraph.com/rss",
         "https://decrypt.co/feed", "https://bitcoinmagazine.com/.rss/full/",
         "https://www.theblock.co/rss.xml"]
ALIAS = {"BTC": ["bitcoin", "btc"], "ETH": ["ethereum", "ether", "eth"], "TRX": ["tron", "trx"],
         "BNB": ["bnb", "binance coin", "bnb chain"], "SOL": ["solana"], "XRP": ["xrp", "ripple"],
         "DOGE": ["dogecoin", "doge"], "ADA": ["cardano"], "TON": ["toncoin"], "LINK": ["chainlink"],
         "AVAX": ["avalanche"], "LTC": ["litecoin"], "DOT": ["polkadot"], "SUI": ["sui"], "NEAR": ["near protocol"], "ATOM": ["cosmos"]}

ALIAS["BCH"] = ["bitcoin cash", "bch"]
ALIAS["NEAR"] = ["near protocol"]
ALIAS["APT"] = ["aptos", "apt"]
ALIAS["ARB"] = ["arbitrum"]
ALIAS["OP"] = ["optimism"]
ALIAS["UNI"] = ["uniswap", "uni"]
ALIAS["ICP"] = ["internet computer", "icp"]
ALIAS["FIL"] = ["filecoin", "fil"]
ALIAS["ETC"] = ["ethereum classic", "etc"]
ALIAS["XLM"] = ["stellar", "xlm"]
ALIAS["HBAR"] = ["hedera", "hbar"]
ALIAS["INJ"] = ["injective", "inj"]
ALIAS["IMX"] = ["immutable x", "imx"]
ALIAS["STX"] = ["stacks stx"]
ALIAS["RUNE"] = ["thorchain", "rune"]
ALIAS["AAVE"] = ["aave", "aave"]
ALIAS["MKR"] = ["makerdao", "mkr"]
ALIAS["SEI"] = ["sei network"]
ALIAS["TIA"] = ["celestia", "tia"]
ALIAS["ALGO"] = ["algorand", "algo"]
ALIAS["VET"] = ["vechain", "vet"]
ALIAS["GRT"] = ["the graph", "grt"]
ALIAS["SAND"] = ["sandbox sand"]
ALIAS["MANA"] = ["decentraland", "mana"]
ALIAS["AXS"] = ["axie infinity", "axs"]
ALIAS["EGLD"] = ["multiversx", "egld"]
ALIAS["KAS"] = ["kaspa", "kas"]
ALIAS["PEPE"] = ["pepe coin", "pepe"]
ALIAS["SHIB"] = ["shiba inu", "shib"]
ALIAS["BONK"] = ["bonk", "bonk"]
ALIAS["WIF"] = ["dogwifhat", "wif"]
ALIAS["FLOKI"] = ["floki", "floki"]
ALIAS["RENDER"] = ["render network", "render"]
ALIAS["FET"] = ["fetch.ai", "fet"]
ALIAS["TAO"] = ["bittensor", "tao"]
ALIAS["WLD"] = ["worldcoin", "wld"]
ALIAS["JUP"] = ["jupiter exchange"]
ALIAS["PYTH"] = ["pyth network", "pyth"]
ALIAS["ENA"] = ["ethena", "ena"]
ALIAS["ONDO"] = ["ondo finance", "ondo"]
ALIAS["LDO"] = ["lido", "ldo"]
ALIAS["CRV"] = ["curve finance", "crv"]
ALIAS["SNX"] = ["synthetix", "snx"]
ALIAS["COMP"] = ["compound finance", "comp"]
ALIAS["GALA"] = ["gala games"]
ALIAS["CHZ"] = ["chiliz", "chz"]
ALIAS["APE"] = ["apecoin"]
ALIAS["ZEC"] = ["zcash", "zec"]
ALIAS["CAKE"] = ["pancakeswap", "cake"]
ALIAS["DYDX"] = ["dydx", "dydx"]
ALIAS["GMX"] = ["gmx", "gmx"]
ALIAS["PENDLE"] = ["pendle", "pendle"]
ALIAS["JTO"] = ["jito", "jto"]
ALIAS["ORDI"] = ["ordinals ordi", "ordi"]
ALIAS["TRB"] = ["tellor", "trb"]
ALIAS["SUSHI"] = ["sushiswap", "sushi"]
ALIAS["KSM"] = ["kusama", "ksm"]
ALIAS["MINA"] = ["mina protocol", "mina"]
ALIAS["ROSE"] = ["oasis network", "rose"]
ALIAS["CFX"] = ["conflux", "cfx"]
ALIAS["OM"] = ["mantra"]
ALIAS["POL"] = ["polygon", "pol"]
ALIAS["XMR"] = ["monero", "xmr"]
ALIAS["ENJ"] = ["enjin", "enj"]
ALIAS["THETA"] = ["theta network", "theta"]
ALIAS["KAVA"] = ["kava", "kava"]
ALIAS["STRK"] = ["starknet", "strk"]
ALIAS["DASH"] = ["dash coin"]
ALIAS["EOS"] = ["eos"]
ALIAS["NEO"] = ["neo blockchain"]
ALIAS["1INCH"] = ["1inch", "1inch"]
ALIAS["ENS"] = ["ethereum name service"]
ALIAS["IOTA"] = ["iota", "iota"]
MARKET = ["crypto", "market", "fed", "federal reserve", "sec", "etf", "inflation", "interest rate", "tariff",
          "stablecoin", "treasury", "cpi", "fomc", "regulat", "bitcoin", "powell", "trump"]
LEX = {"surge": 1, "rally": 1, "soar": 1, "approv": 1, "inflow": 1.2, "adoption": 1, "bullish": 1,
       "record high": 1.5, "all-time high": 1.5, "ath": 1, "jump": 1, "upgrade": .7, "partnership": .5,
       "accumulat": .7, "rate cut": 1.2, "gain": .5, "rebound": .8, "breakout": .8,
       "crash": -1.5, "plunge": -1.2, "drop": -.7, "slump": -1, "hack": -1.5, "exploit": -1.2, "lawsuit": -1,
       "ban": -1.2, "sues": -1.2, "bearish": -1, "liquidat": -1, "outflow": -1.2, "sell-off": -1, "selloff": -1,
       "fraud": -1.5, "rate hike": -1.2, "delist": -1, "collapse": -1.5, "bankrupt": -1.5, "fear": -.5,
       "tumble": -1, "dump": -1, "probe": -.7, "investigation": -.7}


def _has(text, words, full=False):
    return any(re.search(r"\b" + re.escape(w) + (r"\b" if full else ""), text) for w in words)


def _senti(text):
    s = sum(w for k, w in LEX.items() if re.search(r"\b" + re.escape(k), text))
    return max(-2, min(2, s)) / 2


class NewsFeed:
    def __init__(self, refresh_sec=120):
        self.refresh_sec, self.items, self.fng, self.t, self.lock = refresh_sec, [], None, 0, threading.Lock()
        self.llm = {}  # заголовок -> (оценка -1..1, [монеты])

    def refresh(self, force=False):
        with self.lock:
            if not force and time.time() - self.t < self.refresh_sec:
                return
            self.t = time.time()
            items = []
            for url in FEEDS:
                try:
                    r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
                    r.raise_for_status()
                    for it in ET.fromstring(r.content).iter("item"):
                        d = re.sub(r"<[^>]+>", "", it.findtext("description") or "")
                        try:
                            ts = parsedate_to_datetime(it.findtext("pubDate")).timestamp()
                        except Exception:
                            ts = time.time()
                        items.append((it.findtext("title") or "", d, ts))
                except Exception:
                    continue
            if items:
                self.items = items
                self._rate()
            try:
                self.fng = int(requests.get("https://api.alternative.me/fng/?limit=1", timeout=8).json()["data"][0]["value"])
            except Exception:
                pass

    def _rate(self):
        """Если задан ANTHROPIC_API_KEY — Claude оценивает новые заголовки (смысл, а не слова); иначе остаётся словарь."""
        key = os.environ.get("ANTHROPIC_API_KEY")
        new = [t for t, _, ts in sorted(self.items, key=lambda i: -i[2]) if key and t not in self.llm and time.time() - ts < 86400][:50]
        if not new: return
        prompt = ("Оцени влияние каждого заголовка на цену криптовалют в ближайшие часы. Верни ТОЛЬКО JSON-массив той же длины: "
                  '[{"s": число от -1 до 1, "c": ["BTC"]}] где c — тикеры затронутых монет, "ALL" для всего рынка или [].\n'
                  + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(new)))
        try:
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=40,
                              headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                              json={"model": "claude-haiku-5-5", "max_tokens": 3000, "messages": [{"role": "user", "content": prompt}]})
            txt = r.json()["content"][0]["text"]
            arr = json.loads(txt[txt.index("["):txt.rindex("]") + 1])
            for t, a in zip(new, arr): self.llm[t] = (max(-1.0, min(1.0, float(a["s"]))), [str(c).upper() for c in a["c"]])
        except Exception:
            pass

    def score(self, base):
        """-> (score[-1..1] | None, fear_greed, топ-заголовки)"""
        if not self.items:
            return None, self.fng, []
        al = ALIAS.get(base, [base.lower()])
        now, tot, heads = time.time(), 0.0, []
        for t, d, ts in self.items:
            age = (now - ts) / 3600
            if age > 24:
                continue
            x = (t + " " + d).lower()
            if t in self.llm:
                s, cs = self.llm[t]
                rel = 1.0 if base in cs else 0.5 if "ALL" in cs else 0
            else:
                named = _has(x, al, True) if base in ALIAS else bool(re.search(r"\b" + re.escape(base) + r"\b", t + " " + d))
                rel = 1.0 if named else 0.5 if _has(x, MARKET) else 0
                s = _senti(x) if rel else 0
            if not rel or s == 0:
                continue
            w = rel * math.exp(-age / 6)
            tot += w * s
            heads.append((abs(w * s), s, t.strip()))
        sc = math.tanh(tot / 2)
        if self.fng is not None:  # контрарно: крайняя жадность = осторожность
            sc = 0.8 * sc + 0.2 * (-(self.fng - 50) / 50 * 0.6)
        heads.sort(reverse=True)
        return max(-1, min(1, sc)), self.fng, [(s, t) for _, s, t in heads[:3]]
