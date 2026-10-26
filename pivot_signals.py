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
    - SL/TP a jelzes gyertyan:
        BUY : SL = ta.lowest(low, slLookback),  TP = lastPivotHigh
        SELL: SL = ta.highest(high, slLookback), TP = lastPivotLow
    - Jelzes utan a megfelelo waiting flag resetelodik.
    - Volume spike, gyertya-fordulat es SL/TP utani follow-through jelzesek.

Adatforras: Binance publikus REST API (nem kell API kulcs).

Hasznalat:
    python pivot_signals.py                     # SOLUSDT 1h, utolso 1000 gyertya
    python pivot_signals.py --limit 3000
    python pivot_signals.py --plot              # grafikon mentese PNG-be
    python pivot_signals.py --plot --show       # grafikon megnyitasa
    python pivot_signals.py --live              # figyeles: gyertya-zarasra igazodva
    python pivot_signals.py --live --notify     # + push (indulaskor teszt uzenet, majd uj jelek)
    python pivot_signals.py --test-notify       # csak teszt ertesites kuldese
    python pivot_signals.py --notify-once       # egyszeri ellenorzes (cron/CI-hez)

A --live mod a kovetkezo gyertya-zarasra var, es amint az uj gyertya megjelenik a
valaszban (tehat a lezart gyertya vegso adatai megvannak), azonnal ertekel.

Binance Futures megbizasok (lasd trader.py, .env: BINANCE_API_KEY / BINANCE_API_SECRET):
    python pivot_signals.py --account                       # szamla allapot (egyenleg, pozicio)
    python pivot_signals.py --trade --dry-run --notify-once # csak kiirja, mit kuldene
    python pivot_signals.py --trade --testnet --live        # ELES teszt testneten/demon
    python pivot_signals.py --trade --live                  # ELES: jelzes -> megbizas
    python pivot_signals.py --trade --margin-usdt 20 --leverage 3 --live

A --trade / --dry-run csak a --live vagy a --notify-once moddal egyutt ertelmezett.

Az ertesites beallitasa a .env fajlban tortenik (lasd .env.example).
"""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence, Tuple

import requests

import notify
import trader

# A api.binance.com US IP-krol 451-gyel (geo-blokk) elutasit - a GitHub Actions
# runner pedig US-ban fut. A data-api.binance.vision ugyanaz a publikus piaci adat,
# de nincs geo-blokk. Sorrendben probaljuk, az elso mukodo hostot hasznaljuk.
DEFAULT_BASE_URLS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api.binance.us",
]
BASE_URL = DEFAULT_BASE_URLS[0]
KLINES_ENDPOINT = "/api/v3/klines"
# A testnet/demo a futures vegpontot szolgalja ki (mas az utvonal).
FUTURES_KLINES_ENDPOINT = "/fapi/v1/klines"
STATE_FILE = "last_signal.json"
PENDING_FILE = "pending_trade.json"


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
class SpikeMark:
    index: int      # a gyertya indexe, ahol a spike VAN
    time: datetime
    volume: float
    ratio: float    # volume / atlagVol


@dataclass
class Signal:
    index: int
    time: datetime
    kind: str       # "BUY" / "SELL"
    price: float
    # pivot jelzesnel a pivot SL/TP (SL Lookback / utolso pivot), follow jelzesnel
    # a követő SL/TP (jelzett gyertya low/high + legkozelebbi meg nem ert pivot);
    # mindketto a tenyleges trade szint, ami megbizaskent kimegy a tozsdere
    sl: Optional[float] = None   # stop loss szint (SL Lookback gyertya szelsoertek)
    tp: Optional[float] = None   # take profit szint (az utolso megerositett pivot)
    source: str = "pivot"  # "pivot", "volume", "reversal" vagy "follow"
    color: str = ""        # a Pine label színe (a plot_chart ezt használja)


# ═════════════════════════════════════════════════════════════
# SEGEDFUGGVENYEK
# ═════════════════════════════════════════════════════════════

_INTERVAL_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def interval_seconds(interval: str) -> int:
    """'1h' -> 3600, '15m' -> 900, '4h' -> 14400, '1d' -> 86400."""
    unit = interval[-1:].lower()
    if unit not in _INTERVAL_UNITS or not interval[:-1].isdigit():
        raise ValueError(f"Ismeretlen interval: {interval!r}")
    return int(interval[:-1]) * _INTERVAL_UNITS[unit]


# ═════════════════════════════════════════════════════════════
# PINE SZÍNEK ÉS ÉJSZAKAI IDŐABLAK
# ═════════════════════════════════════════════════════════════

# A Pine inputok alapértékei (color.green / color.red / color.blue / color.orange).
COLOR_PIVOT_BUY = "#089981"
COLOR_PIVOT_SELL = "#f23645"
COLOR_VOLUME_BUY = "#2962ff"
COLOR_VOLUME_SELL = "#ff9800"
COLOR_NIGHT = "#000000"          # nightColor
COLOR_WICK = "#ff69b4"           # pinkColor = rgb(255,105,180)
COLOR_FOLLOW_BUY = "#089981"     # ftBuyColor = color.green (mint pivBuyColor)
COLOR_FOLLOW_SELL = "#f23645"    # ftSellColor = color.red (mint pivSellColor)
COLOR_SL_HIT = "#ffeb3b"         # slHitColor = color.yellow
COLOR_SL_CLOSE = "#2962ff"       # slCloseColor = color.blue
COLOR_SPIKE_LABEL = "#ffeb3b"    # spikeLabelColor = color.yellow
COLOR_SPIKE_TEXT = "#000000"     # spikeLabelTextColor = color.black

SPIKE_LABEL_SIZES = {"tiny": 4.5, "small": 5.5, "normal": 6.5, "large": 8.0, "huge": 9.5}

# Pine: a trackedPivotHighs / trackedPivotLows tomb max hossza
TRACKED_PIVOT_LIMIT = 100

# Melyik jelzesekre megy valodi megbizas: a zold/piros cimkek (pivot breakout es
# follow). A kek/narancs (volume, fordulat) es az ejjszakai fekete csak ertesites.
TRADED_SOURCES = ("pivot", "follow")


def _format_volume(value: float) -> str:
    """Pine str.tostring(volume, format.volume) -> '113.9K'."""
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(value) >= size:
            return f"{value / size:.1f}{unit}"
    return f"{value:.0f}"


def _last_sunday_utc(year: int, month: int) -> datetime:
    """Az adott hónap utolsó vasárnapja 01:00 UTC-kor (az EU téli/időnyári váltás)."""
    if month == 12:
        last_day = datetime(year + 1, 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
    else:
        last_day = datetime(year, month + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
    return (last_day - timedelta(days=(last_day.weekday() + 1) % 7)).replace(hour=1)


def budapest_hour(moment: datetime) -> int:
    """
    A gyertya nyitó idejének órája Europe/Budapest időzónában (Pine: hour(time, ...)).

    A zoneinfo csak Python 3.9-től van, ezért az EU-s nyári időszámítás szabályát
    (március utolsó vasárnapja 01:00 UTC-től október utolsó vasárnapja 01:00 UTC-ig
    CEST = UTC+2, egyébként CET = UTC+1) magunk számoljuk.
    """
    utc = moment.astimezone(timezone.utc)
    dst = _last_sunday_utc(utc.year, 3) <= utc < _last_sunday_utc(utc.year, 10)
    return (utc + timedelta(hours=2 if dst else 1)).hour


def is_night_hour(hour: int, night_start: int, night_end: int) -> bool:
    """Pine: nightStart < nightEnd ? [start, end) : [start, 24) ∪ [0, end)."""
    if night_start < night_end:
        return night_start <= hour < night_end
    return hour >= night_start or hour < night_end


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


def _fetch_from_host(session: requests.Session, url: str, symbol: str, interval: str,
                     total: int) -> List[Candle]:
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


def testnet_mode(args) -> bool:
    """Igaz, ha a --testnet vagy a BINANCE_FUTURES_TESTNET=1 be van kapcsolva."""
    if getattr(args, "testnet", False):
        return True
    value = os.environ.get("BINANCE_FUTURES_TESTNET", "").strip().lower()
    return value in ("1", "true", "yes", "on")


def data_source(args, client=None) -> Tuple[List[str], str]:
    """
    (hostok, vegpont) a gyertya-lekerdezeshez.

    - `--base-url`: kezzel megadott host (a klasszikus spot vegponttal),
    - testnet mod (`--testnet` / BINANCE_FUTURES_TESTNET, `--mainnet-data` nelkul):
      a testnet/demo futures piaca, hogy a jelzesek, az SL/TP szintek es a limit
      arak ugyanabbol a piacbol szarmazzanak, ahol a megbizasok mennek,
    - egyebkent a publikus spot hostok (DEFAULT_BASE_URLS).
    """
    if args.base_url:
        return [args.base_url], KLINES_ENDPOINT
    if testnet_mode(args) and not args.mainnet_data:
        hosts: List[str] = []
        if client is not None:
            hosts.append(client.base_url)
        for host in trader.TESTNET_BASE_URLS:
            if host not in hosts:
                hosts.append(host)
        return hosts, FUTURES_KLINES_ENDPOINT
    return list(DEFAULT_BASE_URLS), KLINES_ENDPOINT


def fetch_candles(session: requests.Session, symbol: str, interval: str, total: int,
                  base_url: Optional[str] = None, endpoint: str = KLINES_ENDPOINT,
                  hosts: Optional[List[str]] = None) -> List[Candle]:
    """
    Lekeri a legutobbi `total` db gyertyat (paginálva, max 1000/request).

    Ha `base_url` / `hosts` nincs megadva, sorban probalja a DEFAULT_BASE_URLS
    hostokat. Ez azert kell, mert a api.binance.com bizonyos orszagokbol / US
    IP-krol (pl. GitHub Actions runner) HTTP 451-gyel valaszol.

    Testnet/demo modban a hivas a futures vegpontra megy (`endpoint`), hogy a
    jelzesek ugyanabbol a piacbol szamitodjanak, ahol a megbizasok mennek.
    """
    urls = list(hosts) if hosts else ([base_url] if base_url else list(DEFAULT_BASE_URLS))
    urls = [url.rstrip("/") + endpoint for url in urls]
    last_exc: Optional[Exception] = None

    for url in urls:
        try:
            return _fetch_from_host(session, url, symbol, interval, total)
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            print(f"[data] {url} nem elerheto ({exc}); kovetkezo adatforras...")

    if last_exc is not None:
        raise last_exc
    return []


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

def lowest_low(bars: List[Candle], index: int, lookback: int) -> float:
    """Pine ta.lowest(low, lookback) az `index` gyertyan (az aktualisat is beleertve)."""
    return min(b.low for b in bars[max(0, index - lookback + 1):index + 1])


def highest_high(bars: List[Candle], index: int, lookback: int) -> float:
    """Pine ta.highest(high, lookback) az `index` gyertyan (az aktualisat is beleertve)."""
    return max(b.high for b in bars[max(0, index - lookback + 1):index + 1])


def _atr_at(atr: List[Optional[float]], bars: List[Candle], index: int) -> float:
    """ATR a megadott gyertyán; ha meg nincs ertek (bemelegites), a gyertya terjedelme."""
    value = atr[index] if 0 <= index < len(atr) else None
    return value if value is not None else (bars[index].high - bars[index].low)


def compute_signals(candles: List[Candle], left_h: int = 10, right_h: int = 10,
                    left_l: int = 10, right_l: int = 10, closed_only: bool = False,
                    sl_lookback: int = 5, volume_multiplier: float = 2.0,
                    volume_lookback: int = 10, volume_ma: str = "sma",
                    reversal_lookback: int = 5, reversal_mode: str = "strict",
                    follow_bars: int = 1, night_start: int = 0, night_end: int = 7,
                    wick_ratio: float = 2.0, show_pink_signal: bool = False,
                    hit_marks: Optional[List[Optional[str]]] = None,
                    spikes: Optional[List[SpikeMark]] = None
                    ) -> Tuple[List[Signal], List[PivotMark]]:
    """
    `hit_marks`: ha meg van adva, gyertyánként beírja az SL/TP találat típusát
    ("close" = zárással áttörés, "wick" = csak érintés), ahogy a Pine barcolor.

    `spikes`: ha meg van adva, ide kerül minden volume spike (a Pine SPIKE label
    és a `showSpikeAlert` alapja).

    `show_pink_signal`: a Pine `showPinkSignal` kapcsolója - kikapcsolva a kanóc
    alapú rózsaszín pivot címke nem jelenik meg (a pivot jelzés zöld/piros marad).
    """
    if volume_lookback < 1 or reversal_lookback < 1 or follow_bars < 1:
        raise ValueError("A volume/reversal visszatekintésnek és a follow ablaknak legalább 1-nek kell lennie.")
    if volume_multiplier < 1:
        raise ValueError("A volume szorzó legalább 1 kell legyen.")
    if volume_ma not in ("sma", "wma", "ema"):
        raise ValueError(f"Ismeretlen volume MA típus: {volume_ma!r}")
    if reversal_mode not in ("strict", "majority"):
        raise ValueError(f"Ismeretlen fordulat mód: {reversal_mode!r}")
    if not 0 <= night_start <= 23 or not 0 <= night_end <= 24:
        raise ValueError("Az éjszakai időablak órái 0..23 / 0..24 között lehetnek.")
    if wick_ratio < 1:
        raise ValueError("A kanóc arány legalább 1 legyen.")

    # closed_only: az utolso (meg formálodo) gyertya kihagyasa -> nincs repaint
    bars = candles[:-1] if (closed_only and len(candles) > 1) else candles

    atr = atr_values(bars)

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

    pending_volume_direction = 0
    volume_ema: Optional[float] = None

    in_buy = False
    buy_sl: Optional[float] = None
    buy_tp: Optional[float] = None
    in_sell = False
    sell_sl: Optional[float] = None
    sell_tp: Optional[float] = None

    follow_buy_level: Optional[float] = None
    follow_buy_direction = 0
    follow_buy_remaining = 0
    follow_sell_level: Optional[float] = None
    follow_sell_direction = 0
    follow_sell_remaining = 0

    # Pine: trackedPivotHighs / trackedPivotLows - a meg NEM ERT pivotok.
    # A follow jelzes TP-je a legkozelebbi ilyen szint lesz.
    tracked_highs: List[float] = []
    tracked_lows: List[float] = []

    def next_high_above(price: float) -> Optional[float]:
        above = [p for p in tracked_highs if p > price]
        return min(above) if above else None

    def next_low_below(price: float) -> Optional[float]:
        below = [p for p in tracked_lows if p < price]
        return max(below) if below else None

    signals: List[Signal] = []

    for i, c in enumerate(bars):
        # --- Pivot megerositese -> pivot frissites, hozza tartozo break nullazasa ---
        if ph_conf[i] is not None:
            last_pivot_high = ph_conf[i]
            last_high_break = None
        if pl_conf[i] is not None:
            last_pivot_low = pl_conf[i]
            last_low_break = None

        # --- Uj pivotok a kovetett tombokbe, majd a mar elert szintek torlese.
        #     Igy a follow TP mindig olyan pivot lesz, amit meg semelyik gyertya
        #     nem ert el (Pine "MÁR ELÉRT PIVOTOK ELTÁVOLÍTÁSA"). ---
        if ph_conf[i] is not None:
            tracked_highs.append(ph_conf[i])
            if len(tracked_highs) > TRACKED_PIVOT_LIMIT:
                tracked_highs.pop(0)
        if pl_conf[i] is not None:
            tracked_lows.append(pl_conf[i])
            if len(tracked_lows) > TRACKED_PIVOT_LIMIT:
                tracked_lows.pop(0)
        tracked_highs[:] = [p for p in tracked_highs if c.high < p]
        tracked_lows[:] = [p for p in tracked_lows if c.low > p]

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
        body = abs(c.close - c.open)
        upper_wick = c.high - max(c.close, c.open)
        lower_wick = min(c.close, c.open) - c.low

        is_night = is_night_hour(budapest_hour(c.open_time), night_start, night_end)

        # --- Volume spike és a következő ellentétes gyertya ---
        prior_volumes = [bar.volume for bar in bars[max(0, i - volume_lookback):i]]
        average_volume: Optional[float] = None
        if volume_ma == "sma":
            if len(prior_volumes) == volume_lookback:
                average_volume = sum(prior_volumes) / volume_lookback
        elif volume_ma == "wma":
            if len(prior_volumes) == volume_lookback:
                weight_sum = volume_lookback * (volume_lookback + 1) / 2
                average_volume = sum(
                    volume * weight for weight, volume in enumerate(prior_volumes, start=1)
                ) / weight_sum
        else:
            # Pine ta.ema: az első érték SMA(volume[1], length), utána egy lépés gyertyánként.
            alpha = 2 / (volume_lookback + 1)
            if len(prior_volumes) == volume_lookback:
                if volume_ema is None:
                    volume_ema = sum(prior_volumes) / volume_lookback
                else:
                    volume_ema = alpha * prior_volumes[-1] + (1 - alpha) * volume_ema
                average_volume = volume_ema

        is_volume_spike = average_volume is not None and c.volume > average_volume * volume_multiplier
        if is_volume_spike and spikes is not None:
            ratio = c.volume / average_volume if average_volume else 0.0  # type: ignore[operator]
            spikes.append(SpikeMark(i, c.open_time, c.volume, ratio))
        immediate_buy = is_volume_spike and green_candle and lower_wick > body
        immediate_sell = is_volume_spike and red_candle and upper_wick > body
        pending_volume_buy = pending_volume_direction == -1 and green_candle
        pending_volume_sell = pending_volume_direction == 1 and red_candle
        if pending_volume_buy or pending_volume_sell:
            pending_volume_direction = 0
        if is_volume_spike:
            if immediate_buy or immediate_sell:
                pending_volume_direction = 0
            else:
                pending_volume_direction = 1 if c.close >= c.open else -1
        volume_buy = pending_volume_buy or immediate_buy
        volume_sell = pending_volume_sell or immediate_sell

        # --- Fordulat: az előző gyertyák színének szigorú vagy többségi egyezése ---
        prior_bars = bars[max(0, i - reversal_lookback):i]
        bullish_count = sum(bar.close > bar.open for bar in prior_bars)
        bearish_count = sum(bar.close < bar.open for bar in prior_bars)
        required = (
            reversal_lookback if reversal_mode == "strict"
            else reversal_lookback // 2 + 1
        )
        reversal_buy = bearish_count >= required and green_candle
        reversal_sell = bullish_count >= required and red_candle
        volume_buy = volume_buy or reversal_buy
        volume_sell = volume_sell or reversal_sell

        # --- Pivot jelzések ---
        sell_signal = waiting_for_sell and red_candle
        buy_signal = waiting_for_buy and green_candle

        if sell_signal:
            waiting_for_sell = False
        if buy_signal:
            waiting_for_buy = False

        # --- SL/TP találat után az első irányba megerősítő zárás follow jelzést ad ---
        follow_buy_signal = False
        follow_sell_signal = False
        if follow_buy_remaining > 0 and follow_buy_level is not None:
            if follow_buy_direction == 1 and c.close > follow_buy_level and green_candle:
                follow_buy_signal = True
                follow_buy_remaining = 0
                follow_buy_level = None
                follow_buy_direction = 0
            elif follow_buy_direction == -1 and c.close < follow_buy_level and red_candle:
                follow_sell_signal = True
                follow_buy_remaining = 0
                follow_buy_level = None
                follow_buy_direction = 0
            else:
                follow_buy_remaining -= 1
                if follow_buy_remaining == 0:
                    follow_buy_level = None
                    follow_buy_direction = 0

        if follow_sell_remaining > 0 and follow_sell_level is not None:
            if follow_sell_direction == 1 and c.close > follow_sell_level and green_candle:
                follow_buy_signal = True
                follow_sell_remaining = 0
                follow_sell_level = None
                follow_sell_direction = 0
            elif follow_sell_direction == -1 and c.close < follow_sell_level and red_candle:
                follow_sell_signal = True
                follow_sell_remaining = 0
                follow_sell_level = None
                follow_sell_direction = 0
            else:
                follow_sell_remaining -= 1
                if follow_sell_remaining == 0:
                    follow_sell_level = None
                    follow_sell_direction = 0

        # --- Meglévő pivot pozíciók SL/TP ellenőrzése (SL az elsőbbségi szint) ---
        if in_buy:
            sl_reached = buy_sl is not None and c.low <= buy_sl
            tp_reached = buy_tp is not None and c.high >= buy_tp
            if sl_reached:
                if hit_marks is not None:
                    hit_marks[i] = "close" if c.close < buy_sl else "wick"
                follow_buy_level = buy_sl
                follow_buy_direction = -1
                follow_buy_remaining = follow_bars
                in_buy = False
            elif tp_reached:
                if hit_marks is not None:
                    hit_marks[i] = "close" if c.close > buy_tp else "wick"
                follow_buy_level = buy_tp
                follow_buy_direction = 1
                follow_buy_remaining = follow_bars
                in_buy = False

        if in_sell:
            sl_reached = sell_sl is not None and c.high >= sell_sl
            tp_reached = sell_tp is not None and c.low <= sell_tp
            if sl_reached:
                if hit_marks is not None:
                    hit_marks[i] = "close" if c.close > sell_sl else "wick"
                follow_sell_level = sell_sl
                follow_sell_direction = 1
                follow_sell_remaining = follow_bars
                in_sell = False
            elif tp_reached:
                if hit_marks is not None:
                    hit_marks[i] = "close" if c.close < sell_tp else "wick"
                follow_sell_level = sell_tp
                follow_sell_direction = -1
                follow_sell_remaining = follow_bars
                in_sell = False

        # --- Pine label színek: pivot = zöld/piros (kanócnál rózsaszín, ha a
        #     showPinkSignal be van kapcsolva), volume/fordulat = kék/narancs,
        #     éjszaka fekete, follow = zöld/piros ---
        wicks_sum = upper_wick + lower_wick
        wicks_bigger_than_body = wicks_sum > body
        buy_pink = (
            show_pink_signal and wicks_bigger_than_body and upper_wick > 0
            and upper_wick >= wick_ratio * lower_wick
        )
        sell_pink = (
            show_pink_signal and wicks_bigger_than_body and lower_wick > 0
            and lower_wick >= wick_ratio * upper_wick
        )

        # A Pine egy közös BUY/SELL címkét rajzol, ha pivot- és volume-jelzés
        # ugyanabba az irányba esik; a pivot SL/TP szintjei ilyenkor érvényesek.
        if buy_signal:
            sl_price = lowest_low(bars, i, sl_lookback)
            signals.append(Signal(i, c.open_time, "BUY", c.close,
                                  sl=sl_price, tp=last_pivot_high, source="pivot",
                                  color=COLOR_WICK if buy_pink else COLOR_PIVOT_BUY))
            in_buy = True
            buy_sl = sl_price
            buy_tp = last_pivot_high
            follow_buy_level = None
            follow_buy_direction = 0
            follow_buy_remaining = 0
        elif volume_buy:
            signals.append(Signal(i, c.open_time, "BUY", c.close, source="volume",
                                  color=COLOR_NIGHT if is_night else COLOR_VOLUME_BUY))
        if sell_signal:
            sl_price = highest_high(bars, i, sl_lookback)
            signals.append(Signal(i, c.open_time, "SELL", c.close,
                                  sl=sl_price, tp=last_pivot_low, source="pivot",
                                  color=COLOR_WICK if sell_pink else COLOR_PIVOT_SELL))
            in_sell = True
            sell_sl = sl_price
            sell_tp = last_pivot_low
            follow_sell_level = None
            follow_sell_direction = 0
            follow_sell_remaining = 0
        elif volume_sell:
            signals.append(Signal(i, c.open_time, "SELL", c.close, source="volume",
                                  color=COLOR_NIGHT if is_night else COLOR_VOLUME_SELL))

        # --- Follow jelzés: a Pine a chartra SL/TP vonalat is húz hozzá.
        #     SL = a jelzett gyertya low/high, TP = a legközelebbi MÉG NEM ÉRT
        #     pivot; ha nincs ilyen, ATR-alapú becslés (2×ATR). ---
        if follow_buy_signal:
            ft_tp = next_high_above(c.high)
            if ft_tp is None:
                ft_tp = c.high + 2 * _atr_at(atr, bars, i)
            signals.append(Signal(i, c.open_time, "BUY", c.close, source="follow",
                                  color=COLOR_FOLLOW_BUY, sl=c.low, tp=ft_tp))
        if follow_sell_signal:
            ft_tp = next_low_below(c.low)
            if ft_tp is None:
                ft_tp = c.low - 2 * _atr_at(atr, bars, i)
            signals.append(Signal(i, c.open_time, "SELL", c.close, source="follow",
                                  color=COLOR_FOLLOW_SELL, sl=c.high, tp=ft_tp))

    # pivot jelolok idoben rendezve, valos idovel
    pivots = ph_marks + pl_marks
    for m in pivots:
        m.time = candles[m.index].open_time
    pivots.sort(key=lambda m: m.index)

    return signals, pivots


def signal_params(args) -> dict:
    """A compute_signals paraméterei a parancssori beállításokból."""
    return dict(
        sl_lookback=args.sl_lookback,
        volume_multiplier=args.volume_multiplier,
        volume_lookback=args.volume_lookback,
        volume_ma=args.volume_ma,
        reversal_lookback=args.reversal_lookback,
        reversal_mode=args.reversal_mode,
        follow_bars=args.follow_bars,
        night_start=args.night_start,
        night_end=args.night_end,
        wick_ratio=args.wick_ratio,
        show_pink_signal=args.pink_signal,
    )


# ═════════════════════════════════════════════════════════════
# GRAFIKON (opcionalis, matplotlib)
# ═════════════════════════════════════════════════════════════

def atr_values(candles: List[Candle], length: int = 14) -> List[Optional[float]]:
    """Pine ta.atr(length): RMA a True Range-en (az első érték SMA)."""
    true_ranges: List[float] = []
    previous_close: Optional[float] = None
    for c in candles:
        if previous_close is None:
            true_ranges.append(c.high - c.low)
        else:
            true_ranges.append(max(c.high - c.low,
                                   abs(c.high - previous_close),
                                   abs(c.low - previous_close)))
        previous_close = c.close

    result: List[Optional[float]] = [None] * len(candles)
    if len(true_ranges) < length:
        return result
    result[length - 1] = sum(true_ranges[:length]) / length
    for i in range(length, len(true_ranges)):
        result[i] = (result[i - 1] * (length - 1) + true_ranges[i]) / length  # type: ignore[operator]
    return result


def plot_chart(candles: List[Candle], signals: List[Signal], pivots: List[PivotMark],
               symbol: str, interval: str, out_path: str, show: bool = False,
               hit_marks: Optional[List[Optional[str]]] = None,
               label_offset: float = 0.5, sl_line_length: int = 10,
               spikes: Optional[Sequence[SpikeMark]] = None,
               show_spike_label: bool = True, show_spike_details: bool = True,
               spike_label_color: str = COLOR_SPIKE_LABEL,
               spike_text_color: str = COLOR_SPIKE_TEXT,
               spike_label_size: str = "normal",
               show_pivot_levels: bool = False,
               show_follow_labels: bool = True) -> None:
    try:
        import matplotlib
        if not show:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.lines import Line2D
        from matplotlib.patches import Rectangle
    except ImportError:
        print("A grafikonhoz teleptsd: pip install matplotlib")
        return

    fig, ax = plt.subplots(figsize=(18, 9))

    atr = atr_values(candles)

    def atr_at(index: int) -> float:
        value = atr[index] if 0 <= index < len(atr) else None
        c = candles[index]
        return value if value is not None else (c.high - c.low)

    width = 0.6
    for c in candles:
        color = "#26a69a" if c.close >= c.open else "#ef5350"
        ax.vlines(c.index, c.low, c.high, color=color, linewidth=0.8)
        bottom = min(c.open, c.close)
        height = abs(c.close - c.open) or (c.high - c.low) * 0.001
        ax.add_patch(Rectangle((c.index - width / 2, bottom), width, height,
                               facecolor=color, edgecolor=color, linewidth=0.5))

    # Pine barcolor: SL/TP találat (sárga = csak érintés, kék = zárással áttörés)
    if hit_marks is not None:
        for i, hit in enumerate(hit_marks[:len(candles)]):
            if hit is None:
                continue
            c = candles[i]
            edge = COLOR_SL_CLOSE if hit == "close" else COLOR_SL_HIT
            ax.add_patch(Rectangle((c.index - width / 2 - 0.15, min(c.open, c.close)),
                                   width + 0.3, max(abs(c.close - c.open), (c.high - c.low) * 0.05),
                                   facecolor="none", edgecolor=edge, linewidth=2.0, zorder=4))

    # Pivot cimkek (Pine "Pivot szintek megjelenítése" kapcsoló)
    if show_pivot_levels:
        for m in pivots:
            if m.kind == "high":
                ax.annotate(f"{m.price:.2f}", (m.index, m.price), textcoords="offset points",
                            xytext=(0, 18), ha="center", fontsize=7, color="black",
                            bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="gray", lw=0.5))
                ax.scatter([m.index], [m.price], marker="v", s=22, color="#616161",
                           zorder=6, edgecolors="white", linewidths=0.5)
            else:
                ax.annotate(f"{m.price:.2f}", (m.index, m.price), textcoords="offset points",
                            xytext=(0, -22), ha="center", fontsize=7, color="black",
                            bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="gray", lw=0.5))
                ax.scatter([m.index], [m.price], marker="^", s=22, color="#616161",
                           zorder=6, edgecolors="white", linewidths=0.5)

    def draw_pointer(index: int, candle_y: float, label_y: float, marker: str,
                     color: str, atr: float) -> None:
        """Háromszög-nyíl a gyertya felé + vékony szár a címkéig."""
        gap = max(atr * 0.14, 1e-9)
        tip = candle_y - gap if marker == "^" else candle_y + gap
        ax.vlines(index, min(tip, label_y), max(tip, label_y), color=color,
                  linewidth=1.3, alpha=0.95, zorder=4)
        ax.scatter([index], [tip], marker=marker, s=75, color=color, zorder=7,
                   edgecolors="white", linewidths=0.7)

    # Volume spike címkék (Pine "Volume spike label" csoport)
    if show_spike_label and spikes:
        fontsize = SPIKE_LABEL_SIZES.get(spike_label_size, SPIKE_LABEL_SIZES["normal"])
        for sp in spikes:
            c = candles[sp.index]
            is_up_bar = c.close >= c.open
            # Kicsit kijjebb, mint a BUY/SELL címke, hogy ne lógjanak egymásra
            spike_y = (c.high + atr_at(sp.index) * (label_offset + 0.9) if is_up_bar
                       else c.low - atr_at(sp.index) * (label_offset + 0.9))
            text = ("SPIKE" if not show_spike_details
                    else f"SPIKE\n{_format_volume(sp.volume)}\n×{sp.ratio:.2f}")
            draw_pointer(sp.index, c.high if is_up_bar else c.low, spike_y,
                         "v" if is_up_bar else "^", spike_label_color,
                         atr_at(sp.index))
            ax.annotate(text, (sp.index, spike_y), ha="center",
                        va="bottom" if is_up_bar else "top", fontsize=fontsize,
                        color=spike_text_color, zorder=5, linespacing=0.9,
                        bbox=dict(boxstyle="round,pad=0.2", fc=spike_label_color, ec="none"))

    # Az egymásra lógó SL/TP feliratokat kihagyjuk (a vonal ettől még látszik)
    labelled: List[Tuple[int, float]] = []

    def can_label(index: int, price: float) -> bool:
        for other_index, other_price in labelled:
            if abs(other_index - index) <= 12 and abs(other_price - price) < atr_at(index) * 0.8:
                return False
        labelled.append((index, price))
        return True

    # BUY / SELL címkék a Pine színeivel (a szín hordozza a jel típusát)
    for s in signals:
        if s.source == "follow" and not show_follow_labels:
            continue
        c = candles[s.index]
        color = s.color or (COLOR_PIVOT_BUY if s.kind == "BUY" else COLOR_PIVOT_SELL)
        if s.kind == "BUY":
            y = c.low - atr_at(s.index) * label_offset
            draw_pointer(s.index, c.low, y, "^", color, atr_at(s.index))
            ax.annotate("BUY", (s.index, y), ha="center", va="top", fontsize=7,
                        color="white", fontweight="bold", zorder=6,
                        bbox=dict(boxstyle="round,pad=0.2", fc=color, ec=color, lw=0.5))
        else:
            y = c.high + atr_at(s.index) * label_offset
            draw_pointer(s.index, c.high, y, "v", color, atr_at(s.index))
            ax.annotate("SELL", (s.index, y), ha="center", va="bottom", fontsize=7,
                        color="white", fontweight="bold", zorder=6,
                        bbox=dict(boxstyle="round,pad=0.2", fc=color, ec=color, lw=0.5))

        # SL/TP vonalak (pivot és follow jeleknél - ahogy a Pine), a Pine
        # slLineLength hosszával. A follow jelzés SL/TP-je csak rajz, nem trade.
        if s.source in ("pivot", "follow"):
            end = min(len(candles) - 1, s.index + sl_line_length)
            if s.sl is not None:
                ax.hlines(s.sl, s.index, end, color=COLOR_PIVOT_SELL,
                          linestyle="dashed", linewidth=1.2, zorder=3)
                if can_label(end, s.sl):
                    ax.annotate(f"SL: {s.sl:.2f}", (end, s.sl), xytext=(3, 0),
                                textcoords="offset points", va="center", fontsize=6,
                                color="white", zorder=6,
                                bbox=dict(boxstyle="round,pad=0.15", fc=COLOR_PIVOT_SELL,
                                          ec="none"))
            if s.tp is not None:
                ax.hlines(s.tp, s.index, end, color=COLOR_PIVOT_BUY,
                          linestyle="dashed", linewidth=1.2, zorder=3)
                if can_label(end, s.tp):
                    ax.annotate(f"TP: {s.tp:.2f}", (end, s.tp), xytext=(3, 0),
                                textcoords="offset points", va="center", fontsize=6,
                                color="white", zorder=6,
                                bbox=dict(boxstyle="round,pad=0.15", fc=COLOR_PIVOT_BUY,
                                          ec="none"))

    legend = [
        Line2D([], [], marker="s", linestyle="", markersize=9, color=COLOR_PIVOT_BUY,
               label="Pivot BUY"),
        Line2D([], [], marker="s", linestyle="", markersize=9, color=COLOR_PIVOT_SELL,
               label="Pivot SELL"),
        Line2D([], [], marker="s", linestyle="", markersize=9, color=COLOR_VOLUME_BUY,
               label="Volume/fordulat BUY"),
        Line2D([], [], marker="s", linestyle="", markersize=9, color=COLOR_VOLUME_SELL,
               label="Volume/fordulat SELL"),
    ]
    if any(s.color == COLOR_WICK for s in signals):
        legend.append(Line2D([], [], marker="s", linestyle="", markersize=9,
                             color=COLOR_WICK, label="Pivot kanóc (rózsaszín)"))
    if show_follow_labels and any(s.source == "follow" for s in signals):
        legend.append(Line2D([], [], marker="s", linestyle="", markersize=9,
                             color=COLOR_FOLLOW_BUY, label="Követő (follow) BUY"))
        legend.append(Line2D([], [], marker="s", linestyle="", markersize=9,
                             color=COLOR_FOLLOW_SELL, label="Követő (follow) SELL"))
    legend.append(Line2D([], [], marker="s", linestyle="", markersize=9, color=COLOR_NIGHT,
                         label="Éjszakai volume (00-07 Budapest)"))
    if show_spike_label:
        legend.append(Line2D([], [], marker="s", linestyle="", markersize=9,
                             color=COLOR_SPIKE_LABEL, label="Volume spike (SPIKE címke)"))
    legend.extend([
        Line2D([], [], marker="s", linestyle="", markersize=9, markerfacecolor="none",
               markeredgecolor=COLOR_SL_HIT, markeredgewidth=2, label="SL/TP érintés"),
        Line2D([], [], marker="s", linestyle="", markersize=9, markerfacecolor="none",
               markeredgecolor=COLOR_SL_CLOSE, markeredgewidth=2,
               label="SL/TP zárással áttörés"),
    ])
    ax.legend(handles=legend, loc="upper center", bbox_to_anchor=(0.5, -0.10),
              ncol=4, fontsize=7, framealpha=0.9)

    step = max(1, len(candles) // 12)
    ticks = list(range(0, len(candles), step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([candles[t].open_time.strftime("%m-%d %H:%M") for t in ticks],
                       rotation=45, ha="right", fontsize=8)
    ax.set_xlim(-1, len(candles))
    # A távoli SL/TP szintek ne nyomják össze a gyertyákat: a látható ár-tartományra
    # skálázunk (a kilógó vonalak/címkék levágódnak).
    price_low = min(c.low for c in candles)
    price_high = max(c.high for c in candles)
    pad = max((price_high - price_low) * 0.12, 1e-9)
    ax.set_ylim(price_low - pad, price_high + pad)
    ax.set_title(f"{symbol} {interval} - Volume + Fordulat + Pivot Signals")
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

def run_once(session, args, print_all: bool = True, futures_trader=None,
             closed_only: Optional[bool] = None,
             spikes: Optional[List[SpikeMark]] = None
             ) -> Tuple[List[Signal], List[Candle]]:
    if futures_trader is None:
        futures_trader = _build_trader(args, session)
    client = futures_trader.client if futures_trader is not None else None
    hosts, endpoint = data_source(args, client)
    # Bemelegito (warm-up) gyertyak: a Pine a teljes historiat latja, ezert a
    # jelzesallapot (pending volume, varakozo breakout, nyitott SL/TP) nem
    # nullazodhat a lathato ablak elejen. A warm-up jeleket nem irjuk ki.
    warmup = max(50, args.left + args.right + 50)
    candles = fetch_candles(session, args.symbol, args.interval, args.limit + warmup,
                            endpoint=endpoint, hosts=hosts)
    if not candles:
        print("Nem sikerult gyertyat lekerdezni.")
        return [], []

    if closed_only is None:
        closed_only = getattr(args, "closed_only", False)
    hit_marks: List[Optional[str]] = [None] * len(candles)
    spike_marks: List[SpikeMark] = []
    signals, pivots = compute_signals(candles, args.left, args.right, args.left, args.right,
                                      closed_only=closed_only, hit_marks=hit_marks,
                                      spikes=spike_marks, **signal_params(args))

    offset = max(0, len(candles) - args.limit)
    if offset:
        shown = candles[offset:]
        for i, c in enumerate(shown):
            c.index = i
        signals = [s for s in signals if s.index >= offset]
        for s in signals:
            s.index -= offset
        pivots = [p for p in pivots if p.index >= offset]
        for p in pivots:
            p.index -= offset
        spike_marks = [sp for sp in spike_marks if sp.index >= offset]
        for sp in spike_marks:
            sp.index -= offset
        hit_marks = hit_marks[offset:]
        candles = shown
    if spikes is not None:
        spikes.extend(spike_marks)

    if print_all:
        print(f"\n{args.symbol} {args.interval} | {len(candles)} gyertya "
              f"({candles[0].open_time:%Y-%m-%d %H:%M} -> {candles[-1].open_time:%Y-%m-%d %H:%M} UTC) "
              f"| pivot L/R: {args.left}/{args.right} | SL lookback: {args.sl_lookback}")
        print(f"| volume: {args.volume_multiplier:g}x {args.volume_ma.upper()}"
              f"({args.volume_lookback}) | fordulat: {args.reversal_mode}"
              f"({args.reversal_lookback}) | follow: {args.follow_bars} gyertya")
        buys = sum(1 for s in signals if s.kind == "BUY")
        sells = sum(1 for s in signals if s.kind == "SELL")
        print(f"Jelzesek: {len(signals)} osszesen  (BUY: {buys}, SELL: {sells})")
        print("  Forrasok: " + ", ".join(
            f"{source}: {sum(s.source == source for s in signals)}"
            for source in ("pivot", "volume", "follow")
        ) + "\n")
        for s in signals:
            print(f"  {s.time:%Y-%m-%d %H:%M} UTC  {s.kind:4s} [{_source_tag(s)}] "
                  f"@ {s.price:.2f}{_sl_tp_text(s)}")
        if spike_marks:
            print(f"\nVolume spike-ok: {len(spike_marks)} db")
            for sp in spike_marks:
                print(f"  {sp.time:%Y-%m-%d %H:%M} UTC  vol {_format_volume(sp.volume)} "
                      f"(x{sp.ratio:.2f})")

    if args.plot:
        out = f"pivot_signals_{args.symbol}_{args.interval}.png"
        plot_chart(candles, signals, pivots, args.symbol, args.interval, out,
                   show=args.show, hit_marks=hit_marks,
                   label_offset=args.label_offset,
                   sl_line_length=args.sl_line_length, spikes=spike_marks,
                   show_spike_label=args.spike_label,
                   show_spike_details=args.spike_details,
                   spike_label_color=args.spike_label_color,
                   spike_text_color=args.spike_text_color,
                   spike_label_size=args.spike_label_size,
                   show_pivot_levels=args.pivot_levels,
                   show_follow_labels=args.follow_labels)

    return signals, candles


def _load_last_seen(key: str = "last_signal") -> Optional[datetime]:
    """A legutobb ertesitett jelzes (vagy spike) ideje, hogy ujrainditas utan ne spammeljen."""
    if not os.path.isfile(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return datetime.fromisoformat(json.load(fh)[key])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_last_seen(dt: datetime, key: str = "last_signal") -> None:
    data = {}
    if os.path.isfile(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    data[key] = dt.isoformat()
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError as exc:
        print(f"[notify] allapot mentesi hiba: {exc}")


def _load_state() -> dict:
    """A teljes `last_signal.json` tartalma (hiba eseten ures dict)."""
    if not os.path.isfile(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(data: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError as exc:
        print(f"[notify] allapot mentesi hiba: {exc}")


def _signal_key(signal: Signal) -> str:
    """Egy jelzes azonositoja egy gyertyan belul (forras:irany)."""
    return f"{signal.source}:{signal.kind}"


def _load_intrabar(symbol: str) -> Tuple[Optional[datetime], set]:
    """A zaras elotti rafutasban mar elkuldott jelzesek (gyertya ideje + kulcsok)."""
    entry = _load_state().get("intrabar")
    entry = entry.get(symbol) if isinstance(entry, dict) else None
    if not isinstance(entry, dict):
        return None, set()
    try:
        when = datetime.fromisoformat(entry.get("time"))
    except (TypeError, ValueError):
        return None, set()
    return when, set(entry.get("keys") or [])


def _save_intrabar(symbol: str, when: datetime, keys: set) -> None:
    data = _load_state()
    intrabar = data.get("intrabar")
    if not isinstance(intrabar, dict):
        intrabar = {}
    intrabar[symbol] = {"time": when.isoformat(), "keys": sorted(keys)}
    data["intrabar"] = intrabar
    _save_state(data)


def _load_pending() -> dict:
    """A piacon maradt (meg nem teljesult) belepo limit megbizasok paronkent."""
    if not os.path.isfile(PENDING_FILE):
        return {}
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_pending(state: dict) -> None:
    try:
        with open(PENDING_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except OSError as exc:
        print(f"[trade] a fuggoben levo megbizas allapotat nem sikerult menteni: {exc}")


def _resolve_pending_trade(futures_trader, args) -> None:
    """
    A kovetkezo gyertya-zarasnal lezarja az elozo korben kihelyezett limit belepőt.

    - meg nem jart le: semmit nem tesz,
    - addig nem teljesult: lemondja (es a hozza tartozo arva SL/TP-t is),
    - kozben teljesult: ha a SL/TP meg nem kerult ki, most potolja.
    """
    state = _load_pending()
    entry = state.get(args.symbol)
    if not entry:
        return
    try:
        expires = datetime.fromisoformat(entry["expires_at"])
    except (KeyError, ValueError):
        state.pop(args.symbol, None)
        _save_pending(state)
        return
    if datetime.now(timezone.utc) < expires:
        return

    client = futures_trader.client
    symbol = args.symbol
    cid = entry.get("client_order_id") or None
    oid = entry.get("order_id") or None

    status = "NOT_FOUND"
    try:
        status = client.order_status(symbol, cid, oid or None).status
    except trader.TradingError as exc:
        if "-2013" not in str(exc):
            # Halozati/API hiba: a kovetkezo futas ujra megprobalja, nem veszitjuk el.
            print(f"[trade] a fuggoben levo megbizas allapota nem ellenorizheto: {exc}")
            return
    if status in ("NEW", "PARTIALLY_FILLED"):
        try:
            client.cancel_order(symbol, cid, oid or None)
            price = entry.get("signal_price")
            desc = f" {entry.get('side')} LIMIT @ {price:g}" if price else ""
            print(f"[trade] a kovetkezo gyertya-zarasig nem teljesult:{desc} lemondva")
        except trader.TradingError as exc:
            print(f"[trade] a megbizas lemondasa nem sikerult: {exc}")
            return

    position = client.position(symbol)
    if position.amount == 0:
        cancelled = client.cancel_pivot_orders(symbol)
        if cancelled:
            print(f"[trade] {len(cancelled)} arva SL/TP megbizas lemondva")
    else:
        has_bracket = any(
            str(o.get("clientAlgoId", "")).startswith(trader.PIVOT_CLIENT_PREFIX)
            for o in client.open_algo_orders(symbol)
        )
        if not has_bracket:
            try:
                notes = futures_trader.ensure_brackets(position.amount > 0,
                                                       entry.get("sl"), entry.get("tp"))
            except trader.TradingError as exc:
                print(f"[trade] a belepo teljesult, de a SL/TP potlas nem sikerult: {exc}")
                return
            print("[trade] a belepo teljesult, SL/TP potlas: " + " | ".join(notes))

    state.pop(symbol, None)
    _save_pending(state)


def build_spike_alert(spike: SpikeMark, symbol: str, interval: str):
    """(title, message) egy volume spike ertesiteshez (Pine `showSpikeAlert`)."""
    title = f"{symbol} volume spike"
    message = (
        f"Volume spike - {symbol} {interval}\n"
        f"Mennyiseg: {_format_volume(spike.volume)} (x{spike.ratio:.2f} az atlaghoz)\n"
        f"Gyertya: {spike.time:%Y-%m-%d %H:%M} UTC"
    )
    return title, message


def _source_tag(signal: Signal) -> str:
    """Jelzés-forrás a kimenethez, a Pine label színével (éjszakai / kanóc) kiegészítve."""
    if signal.color == COLOR_NIGHT:
        return "volume/night"
    if signal.color == COLOR_WICK:
        return "pivot/wick"
    return signal.source


def _is_traded(signal: Signal) -> bool:
    """
    Zöld/piros címke -> megy megbízás; a többi (kék/narancs/éjszakai fekete)
    csak értesítés. A zöld/piros a pivot breakout és a követő (follow) jelzés;
    a pivot kanócos (rózsaszín) változata is pivot, tehát szintén kereskedett.
    """
    return signal.source in TRADED_SOURCES


def _sl_tp_text(signal: Signal) -> str:
    """A jelzes SL/TP szintjei egy sorban (csak a kereskedett jelzeseknel van ilyen)."""
    if not _is_traded(signal):
        return ""
    parts = []
    if signal.sl is not None:
        parts.append(f"SL {signal.sl:.2f}")
    if signal.tp is not None:
        parts.append(f"TP {signal.tp:.2f}")
    return ("  " + "  ".join(parts)) if parts else ""


def build_alert(signal: Signal, symbol: str, interval: str, trading: bool = False):
    """
    (title, message) az ertesiteshez. A title ASCII, hogy minden csatorna elbirja.

    `trading`: fut-e a kereskedelem (--trade). A kek/narancs jelzesek mindig csak
    informativak, a zold/piros jelzeseknel jelzi, ha nincs bekapcsolva a trade.
    """
    source_label = {"pivot": "Pivot", "volume": "Volume", "follow": "Follow"}.get(
        signal.source, signal.source
    )
    title = f"{symbol} {signal.kind}" if signal.source == "pivot" else (
        f"{symbol} {source_label} {signal.kind}"
    )
    traded = _is_traded(signal)
    message = (
        f"{source_label} {signal.kind} jelzes - {symbol} {interval}\n"
        f"Ar: {signal.price:.2f}"
        + (f"\nSL: {signal.sl:.2f}" if traded and signal.sl is not None else "")
        + (f"\nTP: {signal.tp:.2f}" if traded and signal.tp is not None else "")
        + f"\nGyertya: {signal.time:%Y-%m-%d %H:%M} UTC"
    )
    if not traded:
        if signal.color == COLOR_NIGHT:
            color_hint = "fekete/ejszakai volume"
        else:
            color_hint = "kek/narancs"
        message += (f"\n\n[Info] Csak ertesites ({color_hint} jelzes) - "
                    "nem nyit es nem zar poziciot.")
    elif not trading:
        message += "\n\n[Info] A kereskedelem kikapcsolva (inditsd --trade kapcsoloval)."
    return title, message


def _build_trader(args, session) -> Optional[trader.FuturesTrader]:
    """FuturesTrader, ha a --trade (vagy --dry-run) be van kapcsolva, kulonben None."""
    if not (getattr(args, "trade", False) or getattr(args, "dry_run", False)):
        return None
    client = trader.build_client(testnet=testnet_mode(args), session=session,
                                 base_url=getattr(args, "futures_url", None))
    # Egy konnyu, csak olvaso hivassal ellenorizzuk a kulcsot es testnet modban
    # kivalasztjuk a hostot (testnet.binancefuture.com vagy demo-fapi.binance.com).
    client.resolve()
    if testnet_mode(args):
        env = os.environ.get("BINANCE_FUTURES_TESTNET", "").strip()
        print(f"[trade] TESTNET mod | host: {client.base_url}"
              + (f" | .env: BINANCE_FUTURES_TESTNET={env}" if env else ""))
    return trader.FuturesTrader(
        client,
        args.symbol,
        leverage=args.leverage,
        margin_usdt=args.margin_usdt,
        order_type=args.order_type,
        limit_offset_bps=args.limit_offset_bps,
        order_timeout=args.order_timeout,
        fallback_market=args.fallback_market,
        sl_tp=args.sl_tp,
        dry_run=args.dry_run,
    )


def _execute_trade(futures_trader, signal: Signal,
                   args) -> Optional[trader.TradeOutcome]:
    """
    A jelzes vegrehajtasa a Futures szamlan; rovid osszefoglaloval ter vissza.

    A zold/piros jelzesek nyitnak/zarnak poziciot, SL/TP szinttel: a pivot breakout
    (SL = SL Lookback gyertya szelsoertek, TP = utolso pivot) es a follow zaras
    (SL = a jelzett gyertya low/high, TP = a legkozelebbi meg nem ert pivot vagy
    2xATR). A kek/narancs (volume/fordulat) es az ejjszakai fekete jelzesek
    kizarolag ertesitest adnak, itt nem tortenik semmi.

    A nyito LIMIT megbizas a jelzes gyertyajanak zaro aran marad a piacon a
    kovetkezo gyertya-zarasig; ha addig nem teljesul, a kovetkezo futas lemondja
    (lasd `_resolve_pending_trade`). Az allapot a `pending_trade.json`-ba kerul.
    """
    if not _is_traded(signal):
        return None
    if futures_trader is None:
        return None

    # Elavult jelzesre nem kereskedunk: kulonben egy ujrainditas (vagy a --live
    # indulasa) az osszes kimaradt jelzest azonnal, regi aron kuldene ki.
    max_age = float(getattr(args, "trade_max_age", 1.0) or 0)
    if max_age > 0:
        step = interval_seconds(args.interval)
        closed = signal.time + timedelta(seconds=step)
        elapsed = (datetime.now(timezone.utc) - closed).total_seconds()
        # A futas par masodperccel a gyertya-zaras utan indul (cron + API
        # kesleltetes), ezert a hatarhoz kis turelmet adunk: a "0 gyertyaval
        # ezelott zart" jelzes is atmenjen, ha eppen most zart a gyertya, es a
        # "1 gyertyaval ezelott zart" (pivot visszaigazolas) ne vesszen el.
        grace = min(60.0, step * 0.1)
        if elapsed > max_age * step + grace:
            age = max(0.0, elapsed) / step
            note = (f"kihagyva: a jelzes gyertyaja {age:.1f} gyertyaval ezelott zart "
                    f"(a limit {max_age:g})")
            print(f"[trade] {note}")
            return trader.TradeOutcome(note)

    try:
        outcome = futures_trader.handle_signal(
            signal.kind, signal.price, sl=signal.sl, tp=signal.tp,
            hold_until_next_candle=True)
    except trader.TradingError as exc:
        print(f"[trade] HIBA: {exc}")
        return trader.TradeOutcome(f"HIBA: {exc}")

    if outcome.pending:
        step = interval_seconds(args.interval)
        outcome.pending["signal_time"] = signal.time.isoformat()
        # A megbizas a kovetkezo gyertya-zarasig el: akkor jar le.
        outcome.pending["expires_at"] = next_close_after(
            datetime.now(timezone.utc), step).isoformat()
        if not getattr(args, "dry_run", False):
            state = _load_pending()
            state[args.symbol] = outcome.pending
            _save_pending(state)
    return outcome


def next_close_after(moment: datetime, step: int) -> datetime:
    """A `moment` utani elso gyertya-zaras idopontja (UTC)."""
    epoch = int(moment.timestamp())
    return datetime.fromtimestamp((epoch // step + 1) * step, tz=timezone.utc)


def wait_for_new_candle(session, args, hosts, endpoint, target: datetime,
                        deadline_seconds: float = 12.0) -> Optional[float]:
    """
    Megvarja, hogy a `target` idoben nyilo (tehat az iment lezart gyertya utani)
    gyertya megjelenjen a valaszban - addig a tozsde a lezart gyertya vegso adatait
    sem feltetlenul publikalta meg.

    150 ms-onkent kerdezunk egy kis limittel, igy a kiertekeles a publikacio
    pillanatahoz a leheto legkozelebb esik. Visszaadja a zaras utani kesleltetest
    masodpercben (None, ha nem sikerult idoben).
    """
    target_ms = int(target.timestamp() * 1000)
    started = time.time()

    while time.time() - started < deadline_seconds:
        try:
            candles = fetch_candles(session, args.symbol, args.interval, 3,
                                    endpoint=endpoint, hosts=hosts)
        except requests.exceptions.RequestException:
            time.sleep(0.2)
            continue
        if candles and int(candles[-1].open_time.timestamp() * 1000) >= target_ms:
            return max(0.0, time.time() - target.timestamp())
        time.sleep(0.15)
    return None


def run_live(session, args) -> None:
    step = interval_seconds(args.interval)
    hosts, endpoint = data_source(args)
    print(f"[LIVE] {args.symbol} {args.interval} - gyertya-zarasra igazodo figyeles "
          f"(Ctrl+C kilepes)\n")

    notifier = notify.Notifier(session)
    if args.notify:
        if notifier.channels:
            print(f"[notify] Aktiv csatornak: {', '.join(notifier.channels)}")
        else:
            print("[notify] FIGYELEM: nincs beallitott csatorna! Masold le a .env.example-t "
                  ".env nevre es toltsd ki.")

        # Indulaskor azonnali teszt ertesites, hogy lassad: el a kapcsolat.
        if notifier.channels:
            started = datetime.now(timezone.utc)
            title = f"{args.symbol} figyeles elindult"
            message = (
                f"Pivot Signals figyeles elindult\n"
                f"Par: {args.symbol} {args.interval}\n"
                f"Ido: {started:%Y-%m-%d %H:%M} UTC\n"
                f"Gyertya-zarasra igazodva ertekel, azonnal amint az uj gyertya megjelenik."
            )
            sent = notifier.send(title, message, priority="default", tags=["rocket"])
            if sent:
                print(f"[notify] Indulo ertesites elkuldve: {', '.join(sent)}")

    last_seen = _load_last_seen()
    if last_seen is not None:
        print(f"[info] Utolso ertesitett jelzes: {last_seen:%Y-%m-%d %H:%M} UTC")
    last_spike_seen = _load_last_seen("last_spike")
    if args.spike_alert and last_spike_seen is not None:
        print(f"[info] Utolso ertesitett volume spike: {last_spike_seen:%Y-%m-%d %H:%M} UTC")

    futures_trader = _build_trader(args, session)
    if futures_trader is not None:
        mode = "DRY-RUN (nem kuld megbizast)" if futures_trader.dry_run else "ELES"
        print(f"[trade] Futures kereskedelem: {mode} | {args.symbol} "
              f"{args.order_type.upper()} | margin {args.margin_usdt:g} USDT x "
              f"{args.leverage} tokeattetel")
        print("[trade] Csak a zold/piros (pivot) jelzesek nyitnak/zarnak poziciot; "
              "a kek/narancs jelzesek csak ertesitest adnak.")
    else:
        print("[info] Kereskedes kikapcsolva (nincs --trade): sem zold, sem piros "
              "jelzesre nem nyilik pozicio, csak ertesites megy.")

    state = {"first_pass": True}

    def evaluate(when: str) -> None:
        """Egy kiertekeles + ertesites/kereskedelem az uj jelzesekre."""
        nonlocal last_seen, last_spike_seen
        if futures_trader is not None and not args.dry_run:
            try:
                _resolve_pending_trade(futures_trader, args)
            except (trader.TradingError, requests.exceptions.RequestException) as exc:
                print(f"[trade] a fuggoben levo megbizas ellenorzese nem sikerult: {exc}")
        spikes: List[SpikeMark] = []
        try:
            signals, candles = run_once(session, args, print_all=False,
                                        futures_trader=futures_trader, closed_only=True,
                                        spikes=spikes)
        except requests.exceptions.RequestException as exc:
            print(f"Hiba a lekeres kozben: {exc}")
            return

        # Elso korben NEM ertesitunk a mar meglevo (multbeli) jelzesekrol,
        # csak beallitjuk a kiindulasi pontot.
        if state["first_pass"] and last_seen is None and signals:
            seen = max(s.time for s in signals)
            if not args.dry_run:
                _save_last_seen(seen)
            print(f"[info] Kiindulasi pont: {seen:%Y-%m-%d %H:%M} UTC "
                  f"(korabbi jelzesek nem ertesitodnek)")
            state["first_pass"] = False
            return
        state["first_pass"] = False

        if args.spike_alert:
            # Az elso futasnal a multbeli spike-ok nem ertesitodnek.
            if last_spike_seen is None:
                if spikes:
                    last_spike_seen = max(sp.time for sp in spikes)
                    print(f"[info] Spike kiindulasi pont: "
                          f"{last_spike_seen:%Y-%m-%d %H:%M} UTC")
            new_spikes = [sp for sp in spikes
                          if last_spike_seen is None or sp.time > last_spike_seen]
            for sp in new_spikes:
                print(f"[{datetime.now(timezone.utc):%H:%M:%S} UTC] VOLUME SPIKE: "
                      f"{sp.time:%Y-%m-%d %H:%M} vol {_format_volume(sp.volume)} "
                      f"(x{sp.ratio:.2f})")
                if args.notify:
                    title, message = build_spike_alert(sp, args.symbol, args.interval)
                    sent = notifier.send(title, message, priority="default",
                                         tags=["bar_chart"])
                    if sent:
                        print(f"  -> ertesites elkuldve: {', '.join(sent)}")
                last_spike_seen = sp.time
                if not args.dry_run:
                    _save_last_seen(last_spike_seen, "last_spike")

        new_signals = [s for s in signals if last_seen is None or s.time > last_seen]
        for s in new_signals:
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"[{ts} UTC] UJ JELZES: {s.time:%Y-%m-%d %H:%M} {s.kind} "
                  f"[{_source_tag(s)}] @ {s.price:.2f}{_sl_tp_text(s)}")
            trade_report = _execute_trade(futures_trader, s, args)
            if not _is_traded(s):
                print("  -> kek/narancs jelzes: csak ertesites, nincs pozicio")
            if args.notify:
                title, message = build_alert(s, args.symbol, args.interval,
                                             trading=futures_trader is not None)
                if trade_report:
                    message += f"\n\n[Trade] {trade_report}"
                sent = notifier.send(title, message, priority="high",
                                     tags=["chart_with_upwards_trend"])
                if sent:
                    print(f"  -> ertesites elkuldve: {', '.join(sent)}")
            last_seen = s.time
            if not args.dry_run:
                _save_last_seen(last_seen)

        price = f"{candles[-1].close:.2f}" if candles else "nincs adat"
        print(f"[{datetime.now(timezone.utc):%H:%M:%S} UTC] {when} | ar: {price} | "
              f"uj jelzes: {len(new_signals)}")

    def sleep_until_next_close() -> datetime:
        """Var a kovetkezo gyertya-zarasig (1 percenkent kiir egy statuszt)."""
        target = next_close_after(datetime.now(timezone.utc), step)
        print(f"[info] Kovetkezo gyertya-zaras: {target:%Y-%m-%d %H:%M:%S} UTC "
              f"({(target - datetime.now(timezone.utc)).total_seconds() / 60:.1f} perc)")

        # 1,5 masodperccel a zaras ELE ebredunk, hogy a helyi ora kisebb elterese
        # (NTP-szinkron utan is lehet par szaz ms) ne kesleltesse a reakciot.
        wake = target - timedelta(seconds=1.5)
        last_status = 0.0
        while True:
            now = datetime.now(timezone.utc)
            if now >= wake:
                return target
            remaining = (wake - now).total_seconds()
            time.sleep(min(1.0, remaining))
            if time.time() - last_status >= 60.0:
                last_status = time.time()
                try:
                    candles = fetch_candles(session, args.symbol, args.interval, 1,
                                            endpoint=endpoint, hosts=hosts)
                except requests.exceptions.RequestException:
                    candles = []
                price = f"{candles[-1].close:.2f}" if candles else "?"
                mins = (wake - datetime.now(timezone.utc)).total_seconds() / 60.0
                print(f"[{datetime.now(timezone.utc):%H:%M:%S} UTC] ar: {price} | "
                      f"zarasig: {mins:.1f} perc")

    # Indulaskor azonnal ertekelunk, hogy egy friss (az iment lezart) jelzes ne
    # varjon a kovetkezo orafordulora.
    evaluate("indulaskor")

    while True:
        target = sleep_until_next_close()
        latency = wait_for_new_candle(session, args, hosts, endpoint, target)
        if latency is None:
            print("[figyelem] az uj gyertya nem jelent meg 12 mp-en belul; "
                  "kiertekeles a kovetkezo korben")
            continue
        evaluate(f"gyertya-zaras utan {latency * 1000:.0f} ms-mal")


def close_boundary_near(moment: datetime, step: int,
                        margin_seconds: float) -> Optional[datetime]:
    """
    Az iment lezart gyertya zarasanak idopontja, ha az `margin_seconds`-en belul
    vagyunk (kulonben None).

    Arra kell, hogy pontosan :00-kor inditva is biztosan a mar publikalt, vegso
    gyertyaadatokat ertekeljuk: a zaras pillanataban a tozsde meg nem feltetlenul
    publikalta az uj gyertyat, es a lezart gyertya adatai sem biztosak.
    """
    epoch = moment.timestamp()
    boundary = (epoch // step) * step
    if epoch - boundary <= margin_seconds:
        return datetime.fromtimestamp(boundary, tz=timezone.utc)
    return None


def run_intrabar_once(session, args, hosts, endpoint, step: int) -> None:
    """
    Rafutas a gyertya-zaras elotti percekre (`--intrabar`).

    A MEG FORMALODO (nem lezart) gyertyat ertekeli, es csak a kek/narancs
    (volume / fordulat / follow) jelzesekrol kuld ertesitest - nem kereskedik.

    A `last_signal` allapotot NEM irja, hogy a zaras utani rendes futas a
    gyertyat feldolgozhassa es a jelzest ismet elkuldje. A mar elkuldott
    jelzeseket csak azert tartja nyilvan, hogy ugyanarra a (nyitott) gyertyara
    a rafutas ne kulddjon ketszer.
    """
    if getattr(args, "trade", False) or getattr(args, "dry_run", False):
        print("[intrabar] rafutas: nem kereskedik, csak ertesitest kuld.")

    now = datetime.now(timezone.utc)
    candles = fetch_candles(session, args.symbol, args.interval, args.limit,
                            endpoint=endpoint, hosts=hosts)
    if not candles:
        print("[intrabar] nem sikerult gyertyat lekerdezni.")
        return

    live = candles[-1]
    close_at = live.open_time + timedelta(seconds=step)
    remaining = (close_at - now).total_seconds() / 60.0
    window = float(getattr(args, "intrabar_minutes", 5.0) or 0)
    if remaining <= 0:
        print(f"[intrabar] a meg nyitott gyertya meg nem jelent meg (az utolso: "
              f"{live.open_time:%Y-%m-%d %H:%M} UTC) - nincs kiertekeles.")
        return
    if remaining > window + 0.5:
        print(f"[intrabar] most nem a zaras elotti {window:g} percben jarunk "
              f"(hatralevo: {remaining:.1f} perc) - nincs kiertekeles.")
        return

    signals, _ = compute_signals(candles, args.left, args.right, args.left, args.right,
                                 closed_only=False, **signal_params(args))
    live_signals = [s for s in signals
                    if s.time == live.open_time and not _is_traded(s)]
    sent_time, sent_keys = _load_intrabar(args.symbol)
    if sent_time is not None and sent_time != live.open_time:
        sent_keys = set()
    fresh = [s for s in live_signals if _signal_key(s) not in sent_keys]

    stamp = f"{live.open_time:%Y-%m-%d %H:%M} UTC"
    if not fresh:
        print(f"[intrabar] nincs uj kek/narancs jelzes a meg nyitott gyertyan ({stamp}).")
        return

    notifier = notify.Notifier(session)
    if not notifier.channels:
        print("HIBA: nincs beallitott ertesitesi csatorna!")
        print("  GitHub: Settings -> Secrets and variables -> Actions -> *Repository secrets*")
        print("  -> New repository secret: NTFY_TOPIC = <a topic neved>")
        sys.exit(1)

    all_sent = True
    for s in fresh:
        print(f"[intrabar] JELZES: {s.kind} [{_source_tag(s)}] @ {s.price:.2f}  ({stamp})")
        title, message = build_alert(s, args.symbol, args.interval, trading=False)
        message += (f"\n\n[Info] Rafutas: a gyertya meg NEM zart le "
                    f"(~{remaining:.0f} perc a zarasig), ezert ez elozetes jelzes - "
                    f"a zaras utan valtozhat.")
        sent = notifier.send(title, message, priority="high",
                             tags=["chart_with_upwards_trend"])
        print(f"  -> ertesites elkuldve: {', '.join(sent) if sent else 'semmi'}")
        if sent:
            sent_keys.add(_signal_key(s))
        else:
            all_sent = False

    if sent_keys:
        _save_intrabar(args.symbol, live.open_time, sent_keys)
    if not all_sent:
        sys.exit(1)


def run_notify_once(session, args) -> None:
    """
    Egyszeri ellenorzes cron/CI ala (pl. GitHub Actions).

    Csak a mar LEZART gyertyakat ertekeli, es ezeket IDO alapon valasztja ki (nem
    a valasz utolso elemei kozul) - igy az sem gond, ha az eppen nyitott gyertya
    meg nem is jelent meg a tozsdei valaszban.

    Ertesites:
      - ha meg nincs allapot (elso futas), csak a legutobbi lezart gyertya,
      - utana MINDEN olyan jelzes, ami ujabb a last_signal.json-ban tarolt idonel:
        igy egy kimaradt (kesett vagy elhalt) futas sem veszit el jelzest.
    """
    hosts, endpoint = data_source(args)
    step = interval_seconds(args.interval)

    if getattr(args, "intrabar", False):
        run_intrabar_once(session, args, hosts, endpoint, step)
        return

    # Pontosan a gyertya-zaras utan inditva (a feladat :00-kor fut) megvarjuk, amig
    # a tozsde az uj gyertyat publikálja - addig a lezart gyertya vegso adatai sem
    # biztosak, es a jelzes kimaradhatna.
    boundary = close_boundary_near(datetime.now(timezone.utc), step, margin_seconds=5.0)
    if boundary is not None:
        waited = wait_for_new_candle(session, args, hosts, endpoint, boundary,
                                     deadline_seconds=10.0)
        if waited is not None:
            print(f"[info] eppen a gyertya-zaras utan indultunk; az uj gyertya "
                  f"{waited * 1000:.0f} ms-mal a zaras utan jelent meg")
        else:
            print("[info] eppen a gyertya-zaras utan indultunk; az uj gyertya 10 mp-en "
                  "belul nem jelent meg - a rendelkezesre allo gyertyakbol ertekelunk")

    candles = fetch_candles(session, args.symbol, args.interval, args.limit,
                            endpoint=endpoint, hosts=hosts)
    if len(candles) < 3:
        print("Nincs eleg gyertya a kiertekeleshez.")
        return

    # Ido alapu szures: csak a mar lezart gyertyakat tartjuk meg.
    now_ts = datetime.now(timezone.utc).timestamp()
    closed = [c for c in candles if c.open_time.timestamp() + step <= now_ts]
    if len(closed) < 3:
        print("Nincs eleg LEZART gyertya a kiertekeleshez.")
        return

    spikes: List[SpikeMark] = []
    signals, _ = compute_signals(closed, args.left, args.right, args.left, args.right,
                                 closed_only=False, spikes=spikes, **signal_params(args))
    last_closed = closed[-1].open_time

    print(f"{args.symbol} {args.interval} | utolso lezart gyertya: "
          f"{last_closed:%Y-%m-%d %H:%M} UTC")
    if not (getattr(args, "trade", False) or getattr(args, "dry_run", False)):
        print("[info] Kereskedes kikapcsolva (nincs --trade): sem zold, sem piros "
              "jelzesre nem nyilik pozicio, csak ertesites megy.")

    # Az elozo korben a piacon hagyott limit belepo lezarasa (a kovetkezo
    # gyertya-zarasnal jart le): teljesult -> SL/TP potlas, egyebkent lemondas.
    futures_trader = None
    if getattr(args, "trade", False) and _load_pending().get(args.symbol):
        try:
            futures_trader = _build_trader(args, session)
            if futures_trader is not None:
                _resolve_pending_trade(futures_trader, args)
        except (trader.TradingError, requests.exceptions.RequestException) as exc:
            print(f"[trade] a fuggoben levo megbizas ellenorzese nem sikerult: {exc}")

    last_seen = _load_last_seen()
    if last_seen is not None and last_closed <= last_seen:
        print(f"Mar feldolgozva ({last_seen:%Y-%m-%d %H:%M} UTC), nincs teendo.")
        return

    notifier = notify.Notifier(session)
    if notifier.channels:
        print(f"Csatornak: {', '.join(notifier.channels)}")
    elif args.dry_run:
        # Dry-run: nem kuldunk semmit, csak a tervet irjuk ki, ezert nem hiba.
        print("Nincs beallitott ertesitesi csatorna - dry-run, csak a tervet irjuk ki.")
    else:
        print("HIBA: nincs beallitott ertesitesi csatorna!")
        print("  GitHub: Settings -> Secrets and variables -> Actions -> *Repository secrets*")
        print("  -> New repository secret: NTFY_TOPIC = <a topic neved>")
        sys.exit(1)

    all_sent = True

    # Opcionalis dinamikus spike ertesites (Pine `showSpikeAlert`): a BUY/SELL
    # jelzesektol fuggetlenul, sajat idobelyeggel (last_spike), hogy ne spamoljon.
    if args.spike_alert:
        last_spike_seen = _load_last_seen("last_spike")
        if last_spike_seen is None:
            fresh_spikes = [sp for sp in spikes if sp.time == last_closed]
            print("Nincs korabbi spike allapot -> csak a legutobbi lezart gyertya szamit.")
        else:
            fresh_spikes = [sp for sp in spikes if sp.time > last_spike_seen]

        for sp in fresh_spikes:
            print(f"VOLUME SPIKE: {sp.time:%Y-%m-%d %H:%M} UTC  vol "
                  f"{_format_volume(sp.volume)} (x{sp.ratio:.2f})")
            title, message = build_spike_alert(sp, args.symbol, args.interval)
            if notifier.channels:
                sent = notifier.send(title, message, priority="default",
                                     tags=["bar_chart"])
                print(f"  -> ertesites elkuldve: {', '.join(sent) if sent else 'semmi'}")
                if not sent:
                    all_sent = False
            else:
                print("  -> nincs ertesitesi csatorna, uzenet kihagyva")

        if fresh_spikes and not args.dry_run and all_sent:
            _save_last_seen(max(sp.time for sp in fresh_spikes), "last_spike")
        elif not fresh_spikes:
            print("Nincs uj volume spike.")

    if last_seen is None:
        # Nincs allapot: csak a legutobbi gyertya, hogy ne spamoljuk a multat.
        fresh = [s for s in signals if s.time == last_closed]
        print("Nincs korabbi allapot -> csak a legutobbi lezart gyertya szamit.")
    else:
        # Potlas: minden jelzes, ami a legutobb ertesitett ido utan keletkezett.
        fresh = [s for s in signals if s.time > last_seen]
        if len(fresh) > 5:
            print(f"FIGYELEM: {len(fresh)} kimaradt jelzes - csak a legutobbi 5 megy ki.")
            fresh = fresh[-5:]

    if not fresh:
        print("Nincs uj jelzes.")
    if futures_trader is None and fresh:
        futures_trader = _build_trader(args, session)
    for s in fresh:
        print(f"JELZES: {s.kind} [{_source_tag(s)}] @ {s.price:.2f}  "
              f"({s.time:%Y-%m-%d %H:%M} UTC){_sl_tp_text(s)}")
        trade_report = _execute_trade(futures_trader, s, args)
        if not _is_traded(s):
            print("  -> kek/narancs jelzes: csak ertesites, nincs pozicio")
        title, message = build_alert(s, args.symbol, args.interval,
                                     trading=futures_trader is not None)
        if trade_report:
            message += f"\n\n[Trade] {trade_report}"
        sent = []
        if notifier.channels:
            sent = notifier.send(title, message, priority="high",
                                 tags=["chart_with_upwards_trend"])
            print(f"  -> ertesites elkuldve: {', '.join(sent) if sent else 'semmi'}")
            if not sent:
                all_sent = False
        else:
            print("  -> nincs ertesitesi csatorna, uzenet kihagyva")

    if all_sent:
        if args.dry_run:
            print("Dry-run: az allapot (last_signal.json) nem valtozik.")
        else:
            _save_last_seen(last_closed)
    else:
        print("HIBA: nem minden ertesites ment ki -> az allapot NEM mentodik, "
              "hogy a kovetkezo futas ujra megprobalja.")
        sys.exit(1)


def run_test_trade(session, args) -> None:
    """
    Onteszt: nyit egy kis poziciot SL/TP-vel, ellenorzi a megbizasokat, majd zarja.

    Ugyanaz a sorozat, mint a kezi teszt: piaci nyitas -> SL/TP algo megbizasok
    ellenorzese -> a sajat megbizasok lemondasa -> pozicio zarasasa. A vegen a
    szamla lapos (nincs pozicio, nincs nyitott megbizas).
    """
    client = trader.build_client(testnet=testnet_mode(args), session=session,
                                 base_url=getattr(args, "futures_url", None))
    host = client.resolve()
    symbol = args.symbol
    print(f"[teszt] Host: {host}")
    print(f"[teszt] Szamla: {client.account_summary()}")

    filters = client.filters(symbol)
    position = client.position(symbol)
    mark = client.mark_price(symbol)
    print(f"[teszt] {symbol} | mark {mark:.2f} | tick {filters.tick_size:g} | "
          f"step {filters.step_size:g} | minNotional {filters.min_notional:g}")
    print(f"[teszt] Nyitott pozicio: {position.amount:+.4f} | "
          f"algo megbizas: {len(client.open_algo_orders(symbol))}")

    if position.amount != 0 and not getattr(args, "force", False):
        print("[teszt] MEGSZAKITVA: van nyitott pozicio, az onteszt lezarna azt.")
        print("[teszt]  Ha ez rendben van, futtasd --force kapcsoloval "
              "(a teszt a vegen lapos allapotban hagyja a szamlat).")
        sys.exit(2)

    if client.dual_side_position():
        print("[teszt] MEGSZAKITVA: a szamla HEDGE (dual side) modban van, "
              "valts ONE-WAY modra.")
        sys.exit(2)

    balance_before = client.account_summary()["wallet"]
    tester = trader.FuturesTrader(client, symbol, leverage=args.leverage,
                                  margin_usdt=args.margin_usdt, order_type="market",
                                  sl_tp=True, order_timeout=10, poll_interval=1)

    # Ha van pozicio, azzal ellentetes iranyban nyitunk (a teszt amugy is zarna).
    kind = "BUY" if position.amount <= 0 else "SELL"
    expected = 1 if kind == "BUY" else -1
    close_side = "SELL" if expected > 0 else "BUY"
    mark = client.mark_price(symbol)
    sl = mark * (0.99 if expected > 0 else 1.01)
    tp = mark * (1.01 if expected > 0 else 0.99)

    print(f"\n[teszt] 1) {kind} nyitas (SL {sl:.2f} / TP {tp:.2f})")
    report = tester.handle_signal(kind, mark, sl=sl, tp=tp)
    print(f"[teszt]    {report}")

    checks: List[Tuple[str, bool, str]] = []
    opened = client.position(symbol)
    algo = client.open_algo_orders(symbol)
    types = [o.get("orderType", "") for o in algo]
    sides = {o.get("side", "") for o in algo}
    checks.append(("pozicio nyitva", opened.amount * expected > 0,
                   f"{opened.amount:+.4f} {symbol}"))
    checks.append(("SL megbizas", any("STOP" in t for t in types),
                   ", ".join(t for t in types if "STOP" in t) or "nincs"))
    checks.append(("TP megbizas", any("TAKE_PROFIT" in t for t in types),
                   ", ".join(t for t in types if "TAKE_PROFIT" in t) or "nincs"))
    checks.append((f"SL/TP oldala ({close_side})",
                   bool(sides) and sides == {close_side}, ", ".join(sorted(sides)) or "nincs"))

    print("\n[teszt] 2) takaritas (megbizasok lemondasa + pozicio zarasa)")
    cancelled = client.cancel_pivot_orders(symbol)
    print(f"[teszt]    lemondott megbizas: {len(cancelled)}")
    current = client.position(symbol)
    if current.amount != 0:
        side = "SELL" if current.amount > 0 else "BUY"
        qty = trader._fmt(trader._round_step_down(abs(current.amount), filters.step_size))
        client.place_order(symbol, side, qty, order_type="MARKET", reduce_only=True,
                           client_order_id=trader._new_client_order_id())
        print(f"[teszt]    zaras: {side} {qty} MARKET (reduceOnly)")

    final = client.position(symbol)
    final_algo = client.open_algo_orders(symbol)
    balance_after = client.account_summary()["wallet"]
    checks.append(("pozicio lezarva", abs(final.amount) < 1e-9, f"{final.amount:+.4f}"))
    checks.append(("nincs nyitott megbizas", len(final_algo) == 0 and not client.open_orders(symbol),
                   f"{len(final_algo)} algo / {len(client.open_orders(symbol))} sima"))

    print("\n[teszt] Eredmenyek:")
    for name, ok, detail in checks:
        print(f"  [{'OK ' if ok else 'HIBA'}] {name}: {detail}")
    print(f"[teszt] Egyenleg valtozas: {balance_after - balance_before:+.4f} USDT "
          f"(a spread/dij miatt; egyenleg {balance_after:.2f} USDT)")

    failed = [name for name, ok, _ in checks if not ok]
    if failed:
        print(f"[teszt] SIKERTELEN: {', '.join(failed)}")
        sys.exit(1)
    print("[teszt] MINDEN ELLENORZES OK - a megbizasok es az SL/TP is mukodik.")


def main() -> None:
    p = argparse.ArgumentParser(description="Volume + Fordulat + Pivot Signals (Pine -> Python)")
    p.add_argument("--symbol", default="SOLUSDT")
    p.add_argument("--interval", default="1h")
    p.add_argument("--limit", type=int, default=1000, help="osszes lekerdezett gyertya")
    p.add_argument("--left", type=int, default=10, help="pivot bal oldali hossz (high es low)")
    p.add_argument("--right", type=int, default=10, help="pivot jobb oldali hossz (high es low)")
    p.add_argument("--sl-lookback", type=int, default=5,
                   help="SL-hez hasznalt gyertyaszam (Pine 'SL Lookback', alapertelmezett: 5)")
    p.add_argument("--volume-multiplier", type=float, default=2.0,
                   help="volume spike küszöb az előző átlag szorzataként (alap: 2)")
    p.add_argument("--volume-lookback", type=int, default=10,
                   help="volume mozgóátlag periódusa (alap: 10)")
    p.add_argument("--volume-ma", choices=["sma", "wma", "ema"], default="sma",
                   help="volume spike mozgóátlag típusa")
    p.add_argument("--reversal-lookback", type=int, default=5,
                   help="fordulatvizsgálat előző gyertyáinak száma (alap: 5)")
    p.add_argument("--reversal-mode", choices=["strict", "majority"], default="strict",
                   help="fordulat: az összes előző gyertya vagy többségük egyezzen")
    p.add_argument("--follow-bars", type=int, default=1,
                   help="SL/TP után ennyi gyertyán belül vár follow-through zárást "
                        "(a Pine-ban 1, azaz csak a következő gyertya)")
    p.add_argument("--wick-ratio", type=float, default=2.0,
                   help="kanóc arány küszöb a rózsaszín pivot jelzéshez (alap: 2)")
    p.add_argument("--show-pink-signal", dest="pink_signal", action="store_true",
                   help="rózsaszín (kanócos) pivot címkék megjelenítése (Pine "
                        "'Rózsaszín pivot jelzés megjelenítése', alapból kikapcsolva)")
    p.add_argument("--pivot-levels", dest="pivot_levels", action="store_true",
                   help="pivot high/low szintcímkék a grafikonon (Pine 'Pivot szintek "
                        "megjelenítése', alapból kikapcsolva)")
    p.add_argument("--no-follow-labels", dest="follow_labels", action="store_false",
                   help="a követő (follow) BUY/SELL címkék elrejtése a grafikonon")
    p.add_argument("--night-start", type=int, default=0,
                   help="éjszakai időablak kezdete, Europe/Budapest óra (alap: 0)")
    p.add_argument("--night-end", type=int, default=7,
                   help="éjszakai időablak vége, Europe/Budapest óra (alap: 7)")
    p.add_argument("--label-offset", type=float, default=0.5,
                   help="BUY/SELL és SPIKE címke távolsága ATR-ben (Pine 'Label távolság')")
    p.add_argument("--sl-line-length", type=int, default=10,
                   help="SL/TP vonal hossza gyertyában (Pine 'SL/TP vonal hossza')")

    # ── Volume spike label (Pine "Volume spike label" csoport) ────────────
    v = p.add_argument_group("Volume spike label")
    v.add_argument("--no-spike-label", dest="spike_label", action="store_false",
                   help="ne rajzolja a SPIKE címkéket a grafikonra")
    v.add_argument("--no-spike-details", dest="spike_details", action="store_false",
                   help="a SPIKE címke csak a szót mutassa (mennyiség és ×arány nélkül)")
    v.add_argument("--spike-label-color", default=COLOR_SPIKE_LABEL,
                   help="SPIKE címke háttere (alap: sárga)")
    v.add_argument("--spike-label-text-color", dest="spike_text_color",
                   default=COLOR_SPIKE_TEXT,
                   help="SPIKE címke szövegszíne (alap: fekete)")
    v.add_argument("--spike-label-size", choices=list(SPIKE_LABEL_SIZES),
                   default="normal", help="SPIKE címke mérete")
    v.add_argument("--spike-alert", action="store_true",
                   help="push értesítés minden volume spike-ról (a BUY/SELL "
                        "jelzésektől függetlenül; Pine 'Dinamikus alert')")
    p.add_argument("--base-url", default=None,
                   help="adatforras host (alapertelmezett: automatikus, "
                        "data-api.binance.vision)")
    p.add_argument("--plot", action="store_true", help="grafikon mentese PNG-be")
    p.add_argument("--show", action="store_true", help="grafikon megnyitasa (a --plot-tal)")
    p.add_argument("--live", action="store_true",
                   help="figyeles a gyertya-zarasokra igazodva (az uj gyertya "
                        "megjelenese pillanataban ertekel)")
    p.add_argument("--notify", action="store_true", help="push ertesites kuldes (--live mellett)")
    p.add_argument("--test-notify", action="store_true", help="csak egy teszt ertesites kuldese")
    p.add_argument("--telegram-chat-id", metavar="BOT_TOKEN", default=None,
                   help="a bot token alapjan kiirja a chat_id-t (elotte irj a botnak "
                        "egy uzenetet Telegramon)")
    p.add_argument("--closed-only", action="store_true",
                   help="egyszeri lekerdezesnel csak lezart gyertyakat szamol "
                        "(a --live mod ezt mindig megteszi)")
    p.add_argument("--notify-once", action="store_true",
                   help="egyszeri ellenorzes + ertesites a legutobbi lezart gyertyara")
    p.add_argument("--intrabar", "--pre-close", dest="intrabar", action="store_true",
                   help="rafutas a zaras elott: a MEG NYITOTT gyertyat ertekeli, es csak "
                        "a kek/narancs (volume/fordulat) jelzesekrol kuld ertesitest "
                        "(--notify-once mellett; nem kereskedik)")
    p.add_argument("--intrabar-minutes", type=float, default=5.0,
                   help="a gyertya-zaras elotti ennyi percen belul ertekel a --intrabar "
                        "(alap: 5)")

    # ── Binance Futures kereskedelem (trader.py) ──────────────────────────
    g = p.add_argument_group("Binance Futures kereskedelem")
    g.add_argument("--trade", action="store_true",
                   help="valodi megbizas kuldese a zold/piros (pivot + follow) jelzesekre "
                        "(enelkul csak jelzes/ertesites)")
    g.add_argument("--dry-run", action="store_true",
                   help="kiirja, milyen megbizas menne ki, de nem kuld semmit")
    g.add_argument("--testnet", action="store_true",
                   help="Binance Futures TESTNET/demo hasznalata (kereskedes ES piaci adat)")
    g.add_argument("--mainnet-data", action="store_true",
                   help="testnet kereskedes mellett is a valodi (mainnet) gyertyakat hasznalja")
    g.add_argument("--futures-url", default=None,
                   help="kezzel megadott futures host (pl. https://demo-fapi.binance.com)")
    g.add_argument("--leverage", type=int, default=1, help="tokeattetel (alapertelmezett: 1)")
    g.add_argument("--margin-usdt", type=float, default=10.0,
                   help="egy poziciora szant margin USDT-ben (alapertelmezett: 10)")
    g.add_argument("--order-type", choices=["limit", "market"], default="limit",
                   help="megbizas tipusa (alapertelmezett: limit)")
    g.add_argument("--limit-offset-bps", type=float, default=0.0,
                   help="limit ar eltolasa a jelzes aratol bps-ben (BUY fel, SELL le)")
    g.add_argument("--order-timeout", type=float, default=20.0,
                   help="ennyi masodpercig var a ZARO megbizas teljesulesere (a nyito "
                        "limit a kovetkezo gyertya-zarasig el)")
    g.add_argument("--fallback-market", action="store_true",
                   help="a zaro lab idotullepesekor a maradek MARKET-tel megy")
    g.add_argument("--no-sl-tp", dest="sl_tp", action="store_false",
                   help="NE tegyen stop-loss / take-profit megbizast a poziciora")
    g.add_argument("--trade-max-age", type=float, default=1.0,
                   help="ennyi gyertyan belul zart jelzesre kereskedjen, a hatart "
                        "beleertve (0 = nincs korlat)")
    g.add_argument("--account", action="store_true",
                   help="a Futures szamla allapotanak kiirasa (egyenleg, pozicio) es kilepes")
    g.add_argument("--test-trade", action="store_true",
                   help="onteszt: nyit egy kis poziciot SL/TP-vel, ellenorzi, majd zarja "
                        "(a vegen lapos allapotban hagyja a szamlat)")
    g.add_argument("--force", action="store_true",
                   help="a --test-trade akkor is fusson, ha van nyitott pozicio "
                        "(azt is lezarja a teszt vegen)")
    args = p.parse_args()
    if args.volume_lookback < 1 or args.reversal_lookback < 1 or args.follow_bars < 1:
        p.error("--volume-lookback, --reversal-lookback és --follow-bars értéke legalább 1 legyen")
    if args.volume_multiplier < 1:
        p.error("--volume-multiplier legalább 1 legyen")
    if args.wick_ratio < 1:
        p.error("--wick-ratio legalább 1 legyen")
    if not (0 <= args.night_start <= 23 and 0 <= args.night_end <= 24):
        p.error("--night-start 0..23, --night-end 0..24 között legyen")
    if args.label_offset < 0:
        p.error("--label-offset nem lehet negatív")
    if args.sl_line_length < 1:
        p.error("--sl-line-length legalább 1 legyen")

    notify.load_env()
    session = requests.Session()

    if args.intrabar and not args.notify_once:
        p.error("--intrabar csak a --notify-once moddal egyutt ertelmezett")
    if args.intrabar_minutes <= 0:
        p.error("--intrabar-minutes nagyobb kell legyen 0-nal")

    if args.test_trade:
        if args.dry_run:
            print("HIBA: a --test-trade valodi megbizasokat kuld, ezert nem "
                  "ertelmezett a --dry-run-nal.")
            print("  Elonezet:  python pivot_signals.py --trade --dry-run --notify-once")
            print("  Onteszt:   python pivot_signals.py --test-trade --testnet")
            sys.exit(2)
        run_test_trade(session, args)
        return

    if (args.trade or args.dry_run) and not (args.live or args.notify_once):
        print("HIBA: a --trade / --dry-run csak a --live vagy a --notify-once moddal egyutt ertelmezett.")
        print("  Elonezet: python pivot_signals.py --trade --dry-run --notify-once")
        print("  Elesben:  python pivot_signals.py --trade --live")
        sys.exit(2)

    if args.account:
        trader.print_account(trader.build_client(testnet=testnet_mode(args), session=session,
                                                 base_url=args.futures_url),
                             args.symbol)
        return

    if args.telegram_chat_id:
        try:
            found = notify.telegram_chat_ids(args.telegram_chat_id, session)
        except (RuntimeError, requests.exceptions.RequestException) as exc:
            print(f"HIBA: {exc}")
            print("  Ellenorizd a tokent (BotFather -> /mybots -> API Token).")
            sys.exit(1)
        if not found:
            print("Nem talaltam chatet. Ird be a botnak Telegramon egy uzenetet "
                  "(pl. /start), majd futtasd ujra ezt a parancsot.")
            sys.exit(1)
        print("Talalt chat_id-k (a .env-be a TELEGRAM_CHAT_ID ertek kell):")
        for chat_id, name in found.items():
            print(f"  TELEGRAM_CHAT_ID={chat_id}   ({name})")
        return

    if args.test_notify:
        nf = notify.Notifier(session)
        print(f"Beallitott csatornak: {nf.channels or 'EGY SEM'}")
        sent = nf.send("Teszt ertesites",
                       "Pivot Signals - ha ezt latod a telefonodon, minden rendben!",
                       priority="high", tags=["test"])
        print(f"Elkuldve: {sent or 'semmi'}")
        if not sent:
            print("HIBA: egyetlen ertesites sem ment ki. Ellenorizd:")
            print("  1) GitHub: Settings -> Secrets and variables -> Actions ful")
            print("     a *Secrets* al-fulon legyen (NE Variables/Codespaces/Dependabot!)")
            print("  2) a nev pontosan: NTFY_TOPIC (repository secret, nem Environment secret)")
            print("  3) az ertek CSAK a topic nev legyen (nincs 'NTFY_TOPIC=', idezojel,")
            print("     szokoz vagy sortores)")
            sys.exit(1)
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
    except trader.TradingError as exc:
        print(f"\n[HIBA] {exc}")
        sys.exit(1)
