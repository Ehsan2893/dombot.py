# Paper-trading simulator (NO real money) - top-100 coins, dominance + technical analysis
# Long/short futures, $20 margin per trade, no TP/SL: every trade is closed after a fixed hold time.
# 9 variants side by side on the same signals: hold 15/30/60 min x leverage 1/3/5.
# Data: CoinGecko (dominance, top 100) + OKX public candles (1m / 15m). One run per execution.
import requests, time, json, os, csv
from datetime import datetime

CG = "https://api.coingecko.com/api/v3"
OKX = "https://www.okx.com/api/v5"
STATE = "dom2_state.json"
HIST = "dom_history.json"
LOG = "dom2_trades.csv"

CFG = dict(
    start_balance=100.0,   # fake dollars
    margin=20.0,           # fake dollars of own money per trade
    max_open=5,            # per variant
    max_new_per_run=2,     # per variant
    maint_pct=0.5,         # maintenance margin used for liquidation
    cooldown_min=60,       # no re-entry in the same coin for this long
    top_n=100,
    dom_window_min=60,     # dominance trend measured over this window
    dom_min_move=0.02,     # min move (percentage points) to call it a trend
    atr15_min=0.35,        # min 15m ATR % (market must be moving enough to beat costs)
    max_dist=0.5,          # max distance % from 1m EMA21 (do not chase)
    vol_x=1.3,             # last 1m volume vs average of previous 20
    fee_pct=0.05,          # futures taker fee per side
)
VARIANTS = {}
for _h in (15, 30, 60):
    for _lv in (1, 3, 5):
        VARIANTS["h%d_x%d" % (_h, _lv)] = dict(hold=_h, lev=_lv)
STABLES = ("usd", "dai", "tusd", "fdusd", "pyusd", "usde", "usdd", "usdg")


def get_json(url, params=None, tries=2):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=20,
                             headers={"User-Agent": "paperbot"})
            if r.status_code == 200:
                return r.json()
            print("HTTP", r.status_code, url[-40:])
            if r.status_code == 429:
                time.sleep(20)
        except Exception as e:
            print("net error:", str(e)[:80])
            time.sleep(2)
    return None


def load(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


def log_trade(row):
    new = not os.path.exists(LOG)
    with open(LOG, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "variant", "symbol", "dir", "entry", "exit",
                        "gross_pct", "roi_pct", "profit_usd", "reason", "macro",
                        "rsi1m", "vol_x", "atr15"])
        w.writerow(row)


# ---------- data ----------
def get_global():
    d = (get_json(CG + "/global") or {}).get("data") or {}
    p = d.get("market_cap_percentage") or {}
    if "btc" not in p or "usdt" not in p or "eth" not in p:
        return None
    others = 100.0 - sum(p.values())
    return [time.time(), p["btc"], p["usdt"], p["eth"], others]


def get_markets():
    data = get_json(CG + "/coins/markets", {
        "vs_currency": "usd", "order": "market_cap_desc",
        "per_page": CFG["top_n"], "page": 1, "sparkline": "false"})
    return data or []


def okx_instruments():
    d = (get_json(OKX + "/public/instruments", {"instType": "SWAP"}) or {}).get("data") or []
    out = {}
    for x in d:
        if x.get("state") == "live" and x.get("settleCcy") == "USDT":
            out[x["instId"].split("-")[0].upper()] = x["instId"]
    return out


def okx_candles(inst, bar, limit=100):
    d = (get_json(OKX + "/market/candles", {"instId": inst, "bar": bar,
                                              "limit": limit}) or {}).get("data") or []
    time.sleep(0.07)
    rows = []
    for c in reversed(d):  # oldest first, confirmed candles only
        if len(c) >= 9 and c[8] == "1":
            rows.append(dict(ts=int(c[0]) // 1000, o=float(c[1]), h=float(c[2]),
                             l=float(c[3]), c=float(c[4]), v=float(c[5])))
    return rows


# ---------- indicators ----------
def ema(vals, n):
    k = 2.0 / (n + 1)
    e = vals[0]
    out = [e]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    g = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    l = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def atr_pct(cs, n=14):
    if len(cs) < n + 1:
        return None
    tr = [max(cs[i]["h"] - cs[i]["l"], abs(cs[i]["h"] - cs[i - 1]["c"]),
              abs(cs[i]["l"] - cs[i - 1]["c"])) for i in range(1, len(cs))]
    a = sum(tr[:n]) / n
    for x in tr[n:]:
        a = (a * (n - 1) + x) / n
    return a / cs[-1]["c"] * 100


def vwap(cs):
    pv = sum((c["h"] + c["l"] + c["c"]) / 3 * c["v"] for c in cs)
    v = sum(c["v"] for c in cs)
    return pv / v if v else None


def trend15_ok(c15, d):
    if len(c15) < 60:
        return None
    cl = [c["c"] for c in c15]
    e20, e50 = ema(cl, 20), ema(cl, 50)
    r, a = rsi(cl), atr_pct(c15)
    if r is None or a is None or a < CFG["atr15_min"]:
        return None
    if d > 0 and cl[-1] > e20[-1] > e50[-1] and e20[-1] > e20[-4] and 50 <= r <= 72:
        return a
    if d < 0 and cl[-1] < e20[-1] < e50[-1] and e20[-1] < e20[-4] and 28 <= r <= 50:
        return a
    return None


def setup1m_ok(c1, d):
    if len(c1) < 60:
        return None
    cl = [c["c"] for c in c1]
    e9, e21 = ema(cl, 9), ema(cl, 21)
    r = rsi(cl)
    vw = vwap(c1[-60:])
    prev = [c["v"] for c in c1[-21:-1]]
    avg = sum(prev) / len(prev) if prev else 0
    vx = c1[-1]["v"] / avg if avg else 0
    dist = abs(cl[-1] / e21[-1] - 1) * 100
    if r is None or vw is None or vx < CFG["vol_x"] or dist > CFG["max_dist"]:
        return None
    last = c1[-1]
    if (d > 0 and e9[-1] > e21[-1] and cl[-1] > vw and 52 <= r <= 70
            and last["c"] > last["o"]):
        return r, vx
    if (d < 0 and e9[-1] < e21[-1] and cl[-1] < vw and 30 <= r <= 48
            and last["c"] < last["o"]):
        return r, vx
    return None


# ---------- dominance ----------
def dom_trend(hist, idx, now):
    cur = hist[-1][idx]
    def at(minutes):
        t = now - minutes * 60
        old = [h for h in hist if h[0] <= t]
        return old[-1][idx] if old else None
    v60, v30 = at(CFG["dom_window_min"]), at(CFG["dom_window_min"] / 2)
    if v60 is None or v30 is None:
        return None
    m = CFG["dom_min_move"]
    if cur - v60 >= m and cur - v30 >= m / 2:
        return 1
    if cur - v60 <= -m and cur - v30 <= -m / 2:
        return -1
    return 0


def macro(hist, now):
    t = {n: dom_trend(hist, i, now) for n, i in
         (("btc", 1), ("usdt", 2), ("eth", 3), ("others", 4))}
    if any(v is None for v in t.values()):
        return None, None, "warming up (need about 60 min of dominance history)"
    s_usdt, s_btc, s_eth, s_oth = -t["usdt"], -t["btc"], t["eth"], t["others"]
    major = s_usdt if (s_usdt == s_btc and s_usdt != 0) else 0
    if major and s_eth == -major:
        major_eth = 0
    else:
        major_eth = major
    alt = s_usdt if (s_usdt == s_btc == s_oth and s_usdt != 0) else 0
    txt = "USDT.D %+d BTC.D %+d ETH.D %+d OTHERS.D %+d -> btc:%+d eth:%+d alts:%+d" % (
        t["usdt"], t["btc"], t["eth"], t["others"], major, major_eth, alt)
    return {"BTC": major, "ETH": major_eth}, alt, txt


# ---------- trading ----------
def slip_pct(rank):
    return 0.02 if rank <= 10 else (0.04 if rank <= 30 else 0.08)


def close_pos(v, name, pos, price, reason):
    lev = VARIANTS[name]["lev"]
    margin = CFG["margin"]
    gross = pos["dir"] * (price / pos["entry"] - 1) * 100
    cost = 2 * (CFG["fee_pct"] + slip_pct(pos["rank"]))
    roi = lev * (gross - cost)
    profit = max(margin * roi / 100, -margin)
    if reason == "LIQUIDATED":
        profit = -margin
    v["balance"] += margin + profit
    side = "LONG" if pos["dir"] > 0 else "SHORT"
    log_trade([datetime.now().strftime("%m-%d %H:%M"), name, pos["sym"], side,
               pos["entry"], price, round(gross, 3), round(profit / margin * 100, 1),
               round(profit, 3), reason, pos["macro"], pos["rsi"], pos["vx"], pos["atr"]])
    print("CLOSE %s %s %s gross %+.2f%% pnl $%.2f (%s)" % (
        name, pos["sym"], side, gross, profit, reason))


def manage_open(v, name, now, cache):
    cfg = VARIANTS[name]
    still = []
    for pos in v["open"]:
        target = pos["entry_open"] + cfg["hold"] * 60
        if now < target + 60 + 8:
            still.append(pos)
            continue
        if pos["inst"] not in cache:
            cache[pos["inst"]] = okx_candles(pos["inst"], "1m", 200)
        cs = cache[pos["inst"]]
        hit = [c for c in cs if c["ts"] >= target]
        if not hit:
            if now > target + 90 * 60 and cs:
                close_pos(v, name, pos, cs[-1]["c"], "LATE")
            else:
                still.append(pos)
            continue
        ex = hit[0]
        win = [c for c in cs if pos["entry_open"] < c["ts"] <= ex["ts"]]
        liq = 100.0 / cfg["lev"] - CFG["maint_pct"]
        if win and cfg["lev"] > 1:
            if pos["dir"] > 0:
                adv = (pos["entry"] - min(c["l"] for c in win)) / pos["entry"] * 100
            else:
                adv = (max(c["h"] for c in win) - pos["entry"]) / pos["entry"] * 100
            if adv >= liq:
                px = pos["entry"] * (1 - pos["dir"] * liq / 100)
                close_pos(v, name, pos, px, "LIQUIDATED")
                continue
        close_pos(v, name, pos, ex["c"], "TIME" if ex["ts"] - target <= 120 else "LATE")
    v["open"] = still


def has_room(v):
    return len(v["open"]) < CFG["max_open"] and v["balance"] >= CFG["margin"]


def find_entries(s, now, dirs, alt, txt):
    for v in s["v"].values():
        v["recent"] = {k: t for k, t in v["recent"].items()
                       if now - t < CFG["cooldown_min"] * 60}
    if not any(has_room(v) for v in s["v"].values()):
        return
    markets = get_markets()
    inst = okx_instruments()
    stage1 = []
    for c in markets:
        sym = (c.get("symbol") or "").upper()
        sid = (c.get("id") or "").lower()
        d = dirs.get(sym, alt) if sym in dirs else alt
        if (not d or sym not in inst
                or any(x in sid for x in STABLES) or sid.startswith(("wrapped", "staked", "bridged"))):
            continue
        free = [v for v in s["v"].values() if has_room(v) and sym not in v["recent"]
                and not any(p["sym"] == sym for p in v["open"])]
        if not free:
            continue
        c15 = okx_candles(inst[sym], "15m", 100)
        a = trend15_ok(c15, d)
        if a is not None:
            stage1.append((c, sym, d, a))
    cands = []
    for c, sym, d, a in stage1:
        c1 = okx_candles(inst[sym], "1m", 100)
        res = setup1m_ok(c1, d)
        if res:
            cands.append((res[1], c, sym, d, a, res[0], c1[-1]))
    cands.sort(key=lambda x: -x[0])
    opened = {name: 0 for name in s["v"]}
    for vx, c, sym, d, a, r, last in cands:
        for name, v in s["v"].items():
            if (opened[name] >= CFG["max_new_per_run"] or not has_room(v)
                    or sym in v["recent"] or any(p["sym"] == sym for p in v["open"])):
                continue
            v["balance"] -= CFG["margin"]
            v["recent"][sym] = now
            v["open"].append(dict(sym=sym, inst=inst[sym], dir=d, entry=last["c"],
                                  entry_open=last["ts"], rank=c.get("market_cap_rank") or 99,
                                  macro=txt.split("->")[0].strip(), rsi=round(r, 1),
                                  vx=round(vx, 2), atr=round(a, 2)))
            opened[name] += 1
        print("SIGNAL %s %s @ %s (rsi %.0f, vol x%.1f, atr15 %.2f%%)" % (
            sym, "LONG" if d > 0 else "SHORT", last["c"], r, vx, a))


def stats(name):
    if not os.path.exists(LOG):
        return "no closed trades yet"
    rows = [r for r in list(csv.reader(open(LOG)))[1:] if r[1] == name]
    n = len(rows)
    if n == 0:
        return "no closed trades yet"
    wins = sum(1 for r in rows if float(r[8]) > 0)
    tot = sum(float(r[8]) for r in rows)
    liqs = sum(1 for r in rows if r[9] == "LIQUIDATED")
    return "trades=%d wins=%d (%.0f%%) liq=%d P/L=$%.2f" % (n, wins, 100 * wins / n, liqs, tot)


def main():
    now = time.time()
    s = load(STATE, {"v": {}})
    for name in VARIANTS:
        s["v"].setdefault(name, {"balance": CFG["start_balance"], "open": [], "recent": {}})
    hist = load(HIST, [])
    snap = get_global()
    if snap:
        hist.append(snap)
    hist = [h for h in hist if now - h[0] < 26 * 3600]
    save(HIST, hist)
    cache = {}
    for name, v in s["v"].items():
        manage_open(v, name, now, cache)
    if snap:
        dirs, alt, txt = macro(hist, now)
        print("macro:", txt)
        if dirs is not None and (any(dirs.values()) or alt):
            find_entries(s, now, dirs, alt, txt)
    else:
        print("no dominance data, skipping entries")
    save(STATE, s)
    for name, v in s["v"].items():
        print("%s: cash=$%.2f open=%d | %s" % (name, v["balance"], len(v["open"]), stats(name)))


if __name__ == "__main__":
    main()
