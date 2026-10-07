"""
"Pivot Breakout + First Candle" (Pivot Signals) - Pine Script v6 -> Python port.

Eredeti Pine logika:
    - ta.pivothigh(leftLenH, rightLenH) / ta.pivotlow(leftLenL, rightLenL)
    - Az utolso megerositett pivot high/low kovetese (lastPivotHigh / lastPivotLow)
    - Breakout:
        highBreak: high > lastPivotHigh  ES  (nincs korabbi break VAGY high > lastHighBreakPrice)
        lowBreak : low  < lastPivotLow   ES  (nincs korabbi break VAGY low  < lastLowBreakPrice)
    - Breakout utan az elso ellentetes gyertya ad jelzest:
        high breakout -> waitingForSell -> elso piros gyertya = SELL
        low  breakout -> waitingForBuy  -> elso zold  gyertya = BUY
    - Jelzes utan a megfelelo waiting flag resetelodik.

Adatforras: Binance publikus REST API (nem kell API kulcs).

Hasznalat:
    python pivot_signals.py                     # SOLUSDT 1h, utolso 1000 gyertya
    python pivot_signals.py --limit 3000
    python pivot_signals.py --plot              # grafikon mentese PNG-be
    python pivot_signals.py --plot --show       # grafikon megnyitasa
    python pivot_signals.py --live              # masodpercenkenti figyeles
    python pivot_signals.py --live --notify     # + push ertesites a telefonra
    python pivot_signals.py --test-notify       # csak teszt ertesites kuldese
    python pivot_signals.py --notify-once       # egyszeri ellenorzes (cron/CI-hez)

Az ertesites beallitasa a .env fajlban tortenik (lasd .env.example).
"""

import argparse
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

import requests

import notify

BASE_URL = "https://api.binance.com"
KLINES_ENDPOINT = "/api/v3/klines"
STATE_FILE = "last_signal.json"


# ═════════════════════════════════════════════════════════════
# ADATMODELLEK
# ═════════════════════════════════════════════════════════════

@dataclass
class Candle:
    index: int
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class PivotMark:
    index: int      # a gyertya indexe, ahol a pivot VAN (nem a megerosites helye)
    time: datetime
    price: float
    kind: str       # "high" / "low"


@dataclass
class Signal:
    index: int
    time: datetime
    kind: str       # "BUY" / "SELL"
    price: float


# ═════════════════════════════════════════════════════════════
# BINANCE LEKERES
# ═════════════════════════════════════════════════════════════

def _parse_kline(raw: Sequence, index: int) -> Candle:
    return Candle(
        index=index,
        open_time=datetime.fromtimestamp(raw[0] / 1000, tz=timezone.utc),
        open=float(raw[1]),
        high=float(raw[2]),
        low=float(raw[3]),
        close=float(raw[4]),
        volume=float(raw[5]),
    )


def fetch_candles(session: requests.Session, symbol: str, interval: str, total: int,
                  base_url: str = BASE_URL) -> List[Candle]:
    """Lekeri a legutobbi `total` db gyertyat (paginálva, max 1000/request)."""
    url = base_url + KLINES_ENDPOINT
    candles: List[Candle] = []
    end_time: Optional[int] = None

    while len(candles) < total:
        batch = min(1000, total - len(candles))
        params = {"symbol": symbol, "interval": interval, "limit": batch}
        if end_time is not None:
            params["endTime"] = end_time

        resp = session.get(url, params=params, timeout=10)
        resp.raise_for_status()
        raw = resp.json()
        if not raw:
            break

        parsed = [_parse_kline(k, 0) for k in raw]
        candles = parsed + candles

        if len(raw) < batch:
            break
        end_time = raw[0][0] - 1  # az elso gyertya elott folytatjuk

    candles = candles[-total:]
    for i, c in enumerate(candles):
        c.index = i
    return candles


# ═════════════════════════════════════════════════════════════
# PIVOTOK  (ta.pivothigh / ta.pivotlow ekvivalens)
# ═════════════════════════════════════════════════════════════

def find_pivots(values: Sequence[float], left: int, right: int, kind: str
                ) -> Tuple[List[Optional[float]], List[PivotMark]]:
    """
    Visszaadja:
      confirmed[i] = a pivot erteke, ha az `i` indexu gyertyan megerosodott (kulonben None)
      marks        = a pivotok listaja (a pivot gyertya indexevel)

    A pivot az `i - right` indexu gyertyan van.
    Szigoru osszehasonlitas, ahogy a Pine ta.pivothigh/pivotlow is tesz:
      - pivot HIGH: a csucs nagyobb kell legyen MINDEN szomszednal  -> elutasitas: value >= v
      - pivot LOW : a melypont kisebb kell legyen MINDEN szomszednal -> elutasitas: value <= v
    """
    n = len(values)
    confirmed: List[Optional[float]] = [None] * n
    marks: List[PivotMark] = []
    is_high = kind == "high"

    for i in range(left + right, n):
        p = i - right
        v = values[p]
        is_pivot = True

        for j in range(p - left, p):
            if (values[j] >= v) if is_high else (values[j] <= v):
                is_pivot = False
                break
        if is_pivot:
            for j in range(p + 1, p + right + 1):
                if (values[j] >= v) if is_high else (values[j] <= v):
                    is_pivot = False
                    break

        if is_pivot:
            confirmed[i] = v
            marks.append(PivotMark(index=p, time=None, price=v, kind=kind))  # type: ignore[arg-type]

    return confirmed, marks


# ═════════════════════════════════════════════════════════════
# JELZES SZAMITAS
# ═════════════════════════════════════════════════════════════

def compute_signals(candles: List[Candle], left_h: int = 10, right_h: int = 10,
                    left_l: int = 10, right_l: int = 10, closed_only: bool = False
                    ) -> Tuple[List[Signal], List[PivotMark]]:
    # closed_only: az utolso (meg formálodo) gyertya kihagyasa -> nincs repaint
    bars = candles[:-1] if (closed_only and len(candles) > 1) else candles

    highs = [c.high for c in bars]
    lows = [c.low for c in bars]

    ph_conf, ph_marks = find_pivots(highs, left_h, right_h, "high")
    pl_conf, pl_marks = find_pivots(lows, left_l, right_l, "low")

    last_pivot_high: Optional[float] = None
    last_pivot_low: Optional[float] = None

    last_high_break: Optional[float] = None
    last_low_break: Optional[float] = None

    waiting_for_sell = False
    waiting_for_buy = False

    signals: List[Signal] = []

    for i, c in enumerate(bars):
        # --- Pivot megerositese -> pivot frissites, hozza tartozo break nullazasa ---
        if ph_conf[i] is not None:
            last_pivot_high = ph_conf[i]
            last_high_break = None
        if pl_conf[i] is not None:
            last_pivot_low = pl_conf[i]
            last_low_break = None

        # --- Breakout ---
        high_break = (
            last_pivot_high is not None
            and c.high > last_pivot_high
            and (last_high_break is None or c.high > last_high_break)
        )
        low_break = (
            last_pivot_low is not None
            and c.low < last_pivot_low
            and (last_low_break is None or c.low < last_low_break)
        )

        if high_break:
            last_high_break = c.high
            waiting_for_sell = True
            waiting_for_buy = False

        if low_break:
            last_low_break = c.low
            waiting_for_buy = True
            waiting_for_sell = False

        # --- Gyertyatípus ---
        green_candle = c.close > c.open
        red_candle = c.close < c.open

        # --- Jelzesek ---
        sell_signal = waiting_for_sell and red_candle
        buy_signal = waiting_for_buy and green_candle

        if sell_signal:
            waiting_for_sell = False
            signals.append(Signal(i, c.open_time, "SELL", c.close))
        if buy_signal:
            waiting_for_buy = False
            signals.append(Signal(i, c.open_time, "BUY", c.close))

    # pivot jelolok idoben rendezve, valos idovel
    pivots = ph_marks + pl_marks
    for m in pivots:
        m.time = candles[m.index].open_time
    pivots.sort(key=lambda m: m.index)

    return signals, pivots


# ═════════════════════════════════════════════════════════════
# GRAFIKON (opcionalis, matplotlib)
# ═════════════════════════════════════════════════════════════

def plot_chart(candles: List[Candle], signals: List[Signal], pivots: List[PivotMark],
               symbol: str, interval: str, out_path: str, show: bool = False) -> None:
    try:
        import matplotlib
        if not show:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
    except ImportError:
        print("A grafikonhoz teleptsd: pip install matplotlib")
        return

    fig, ax = plt.subplots(figsize=(18, 9))

    width = 0.6
    for c in candles:
        color = "#26a69a" if c.close >= c.open else "#ef5350"
        ax.vlines(c.index, c.low, c.high, color=color, linewidth=0.8)
        bottom = min(c.open, c.close)
        height = abs(c.close - c.open) or (c.high - c.low) * 0.001
        ax.add_patch(Rectangle((c.index - width / 2, bottom), width, height,
                               facecolor=color, edgecolor=color, linewidth=0.5))

    # Pivot cimkek
    for m in pivots:
        if m.kind == "high":
            ax.annotate(f"{m.price:.2f}", (m.index, m.price), textcoords="offset points",
                        xytext=(0, 10), ha="center", fontsize=7, color="black",
                        bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="gray", lw=0.5))
        else:
            ax.annotate(f"{m.price:.2f}", (m.index, m.price), textcoords="offset points",
                        xytext=(0, -14), ha="center", fontsize=7, color="black",
                        bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="gray", lw=0.5))

    # BUY / SELL jelzesek
    for s in signals:
        c = candles[s.index]
        if s.kind == "BUY":
            ax.scatter(s.index, c.low, marker="^", s=140, color="green", zorder=5)
            ax.annotate("BUY", (s.index, c.low), textcoords="offset points", xytext=(0, -18),
                        ha="center", fontsize=8, color="green", fontweight="bold")
        else:
            ax.scatter(s.index, c.high, marker="v", s=140, color="red", zorder=5)
            ax.annotate("SELL", (s.index, c.high), textcoords="offset points", xytext=(0, 12),
                        ha="center", fontsize=8, color="red", fontweight="bold")

    step = max(1, len(candles) // 12)
    ticks = list(range(0, len(candles), step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([candles[t].open_time.strftime("%m-%d %H:%M") for t in ticks],
                       rotation=45, ha="right", fontsize=8)
    ax.set_xlim(-1, len(candles))
    ax.set_title(f"{symbol} {interval} - Pivot Breakout + First Candle")
    ax.grid(alpha=0.25)
    fig.tight_layout()

    if show:
        plt.show()
    else:
        fig.savefig(out_path, dpi=110)
        print(f"Grafikon elmentve: {out_path}")
    plt.close(fig)


# ═════════════════════════════════════════════════════════════
# FUTTATAS
# ═════════════════════════════════════════════════════════════

def run_once(session, args, print_all: bool = True) -> Tuple[List[Signal], List[Candle]]:
    candles = fetch_candles(session, args.symbol, args.interval, args.limit, args.base_url)
    if not candles:
        print("Nem sikerult gyertyat lekerdezni.")
        return [], []

    signals, pivots = compute_signals(candles, args.left, args.right, args.left, args.right,
                                      closed_only=getattr(args, "closed_only", False))

    if print_all:
        print(f"\n{args.symbol} {args.interval} | {len(candles)} gyertya "
              f"({candles[0].open_time:%Y-%m-%d %H:%M} -> {candles[-1].open_time:%Y-%m-%d %H:%M} UTC) "
              f"| pivot L/R: {args.left}/{args.right}")
        buys = sum(1 for s in signals if s.kind == "BUY")
        sells = sum(1 for s in signals if s.kind == "SELL")
        print(f"Jelzesek: {len(signals)} osszesen  (BUY: {buys}, SELL: {sells})\n")
        for s in signals:
            print(f"  {s.time:%Y-%m-%d %H:%M} UTC  {s.kind:4s}  @ {s.price:.2f}")

    if args.plot:
        out = f"pivot_signals_{args.symbol}_{args.interval}.png"
        plot_chart(candles, signals, pivots, args.symbol, args.interval, out, show=args.show)

    return signals, candles


def _load_last_seen() -> Optional[datetime]:
    """A legutobb ertesitett jelzes ideje (hogy ujrainditas utan ne spammeljen)."""
    if not os.path.isfile(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return datetime.fromisoformat(json.load(fh)["last_signal"])
    except (OSError, ValueError, KeyError):
        return None


def _save_last_seen(dt: datetime) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump({"last_signal": dt.isoformat()}, fh)
    except OSError as exc:
        print(f"[notify] allapot mentesi hiba: {exc}")


def build_alert(signal: Signal, symbol: str, interval: str):
    """(title, message) az ertesiteshez. A title ASCII, hogy minden csatorna elbirja."""
    title = f"{symbol} {signal.kind}"
    message = (
        f"{signal.kind} jelzes - {symbol} {interval}\n"
        f"Ar: {signal.price:.2f}\n"
        f"Gyertya: {signal.time:%Y-%m-%d %H:%M} UTC"
    )
    return title, message


def run_live(session, args) -> None:
    print(f"[LIVE] {args.symbol} {args.interval} - masodpercenkenti figyeles (Ctrl+C kilepes)\n")

    notifier = notify.Notifier(session)
    if args.notify:
        if notifier.channels:
            print(f"[notify] Aktiv csatornak: {', '.join(notifier.channels)}")
        else:
            print("[notify] FIGYELEM: nincs beallitott csatorna! Masold le a .env.example-t "
                  ".env nevre es toltsd ki.")

    last_seen = _load_last_seen()
    if last_seen is not None:
        print(f"[info] Utolso ertesitett jelzes: {last_seen:%Y-%m-%d %H:%M} UTC")

    first_pass = True
    while True:
        try:
            signals, candles = run_once(session, args, print_all=False)

            # Elso korben NEM ertesitunk a mar meglevo (multbeli) jelzesekrol,
            # csak beallitjuk a kiindulasi pontot.
            if first_pass and last_seen is None and signals:
                last_seen = max(s.time for s in signals)
                _save_last_seen(last_seen)
                print(f"[info] Kiindulasi pont: {last_seen:%Y-%m-%d %H:%M} UTC "
                      f"(korabbi jelzesek nem ertesitodnek)")
            first_pass = False

            for s in signals:
                if last_seen is not None and s.time <= last_seen:
                    continue
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"[{ts} UTC] UJ JELZES: {s.time:%Y-%m-%d %H:%M} {s.kind} @ {s.price:.2f}")
                if args.notify:
                    title, message = build_alert(s, args.symbol, args.interval)
                    sent = notifier.send(title, message, priority="high",
                                         tags=["chart_with_upwards_trend"])
                    if sent:
                        print(f"  -> ertesites elkuldve: {', '.join(sent)}")
                last_seen = s.time
                _save_last_seen(last_seen)

            state = (f"ar: {candles[-1].close:.2f} | gyertya: "
                     f"{candles[-1].open_time:%Y-%m-%d %H:%M}") if candles else "nincs adat"
            print(f"[{datetime.now(timezone.utc):%H:%M:%S} UTC] {state}")

        except requests.exceptions.RequestException as exc:
            print(f"Hiba a lekeres kozben: {exc}")
        time.sleep(1)


def run_notify_once(session, args) -> None:
    """
    Egyszeri ellenorzes cron/CI ala (pl. GitHub Actions).

    Csak a legutobbi LEZART gyertya jelzeserol ertesit, es a last_signal.json
    segitsegevel gondoskodik rola, hogy ugyanaz a gyertya ne ertesitsen ketszer.
    """
    candles = fetch_candles(session, args.symbol, args.interval, args.limit, args.base_url)
    if len(candles) < 3:
        print("Nincs eleg gyertya a kiértékeléshez.")
        return

    signals, _ = compute_signals(candles, args.left, args.right, args.left, args.right,
                                 closed_only=True)
    last_closed = candles[-2].open_time

    print(f"{args.symbol} {args.interval} | utolso lezart gyertya: "
          f"{last_closed:%Y-%m-%d %H:%M} UTC")

    last_seen = _load_last_seen()
    if last_seen is not None and last_closed <= last_seen:
        print(f"Mar feldolgozva ({last_seen:%Y-%m-%d %H:%M} UTC), nincs teendo.")
        return

    fresh = [s for s in signals if s.time == last_closed]

    notifier = notify.Notifier(session)
    if notifier.channels:
        print(f"Csatornak: {', '.join(notifier.channels)}")
    else:
        print("FIGYELEM: nincs beallitott ertesitesi csatorna (lasd .env.example).")

    if not fresh:
        print("Nincs jelzes ezen a gyertyan.")
    for s in fresh:
        print(f"JELZES: {s.kind} @ {s.price:.2f}")
        title, message = build_alert(s, args.symbol, args.interval)
        sent = notifier.send(title, message, priority="high",
                             tags=["chart_with_upwards_trend"])
        print(f"  -> ertesites elkuldve: {', '.join(sent) if sent else 'semmi'}")

    _save_last_seen(last_closed)


def main() -> None:
    p = argparse.ArgumentParser(description="Pivot Breakout + First Candle (Pine -> Python)")
    p.add_argument("--symbol", default="SOLUSDT")
    p.add_argument("--interval", default="1h")
    p.add_argument("--limit", type=int, default=1000, help="osszes lekerdezett gyertya")
    p.add_argument("--left", type=int, default=10, help="pivot bal oldali hossz (high es low)")
    p.add_argument("--right", type=int, default=10, help="pivot jobb oldali hossz (high es low)")
    p.add_argument("--base-url", default=BASE_URL)
    p.add_argument("--plot", action="store_true", help="grafikon mentese PNG-be")
    p.add_argument("--show", action="store_true", help="grafikon megnyitasa (a --plot-tal)")
    p.add_argument("--live", action="store_true", help="masodpercenkenti figyeles")
    p.add_argument("--notify", action="store_true", help="push ertesites kuldes (--live mellett)")
    p.add_argument("--test-notify", action="store_true", help="csak egy teszt ertesites kuldese")
    p.add_argument("--closed-only", action="store_true",
                   help="csak lezart gyertyakat szamol (nincs repaint, de kesobb jelez)")
    p.add_argument("--notify-once", action="store_true",
                   help="egyszeri ellenorzes + ertesites a legutobbi lezart gyertyara")
    args = p.parse_args()

    notify.load_env()
    session = requests.Session()

    if args.test_notify:
        nf = notify.Notifier(session)
        print(f"Beallitott csatornak: {nf.channels or 'EGY SEM'}")
        sent = nf.send("Teszt ertesites",
                       "Pivot Signals - ha ezt latod a telefonodon, minden rendben!",
                       priority="high", tags=["test"])
        print(f"Elkuldve: {sent or 'semmi'}")
        return

    if args.notify_once:
        run_notify_once(session, args)
        return

    if args.live:
        run_live(session, args)
    else:
        run_once(session, args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nLeallitva.")
