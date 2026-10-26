"""
Binance USDT-M Futures megbizas-kuldes a Pivot Signals jelzesek alapjan.

Logika (one-way / egyiranyu pozicio mod):
    BUY  jelzes -> a meglevo SHORT zarasa, majd LONG nyitas
    SELL jelzes -> a meglevo LONG zarasa,  majd SHORT nyitas

A nyito LIMIT megbizas a jelzes gyertyajanak zaro aran megy ki, es (ha nem
teljesul azonnal) a piacon marad a kovetkezo gyertya-zarasig - a lemondas a
hivo (pivot_signals) dolga, `hold_until_next_candle=True` eseten.

A jelzes SL/TP szintjeire (mar a belepo kihelyezesekor) bracket megbizas kerul:
    STOP_MARKET          stopPrice = SL  (closePosition=true)
    TAKE_PROFIT_MARKET   stopPrice = TP  (closePosition=true)
A closePosition=true miatt a triggereles a teljes poziciot zarja, es a par
tobbi nyitott megbizasat is lemondja - igy nem marad arva SL a TP utan.
Uj jelzesnel (valtas) a korabbi bracket megbizasok lemondasra kerulnek.

Ha mar a jelzes iranyaban all a pozicio, nem tortenik semmi (nincs
veletlen tokeattetel-noveles).

Beallitas a .env fajlban:
    BINANCE_API_KEY=...
    BINANCE_API_SECRET=...
    # Opcionalis: testnet hasznalata (https://testnet.binancefuture.com)
    BINANCE_FUTURES_TESTNET=1

BIZTONSAG:
  - Az API kulcson csak "Enable Futures" jog legyen; "Withdraw" SOHA.
  - Ha tudod, IP-szuressel add meg a kulcsot.
  - Eloszor MINDIG testneten probald (--testnet).
  - Valodi megbizas csak a --trade flaggel megy ki; a --dry-run csak kiirja a tervet.
"""

import hashlib
import hmac
import itertools
import os
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

FUTURES_BASE_URL = "https://fapi.binance.com"
# A Binance a testnet webes feluletet atiranyitja a demo.binance.com-ra, de a
# klasszikus testnet API tovabbra is el. A ket kornyezet KULON szamla/kulcs,
# ezert az elso alart hivas donti el, melyik fogadja el a kulcsot.
FUTURES_TESTNET_BASE_URL = "https://testnet.binancefuture.com"
FUTURES_DEMO_BASE_URL = "https://demo-fapi.binance.com"
TESTNET_BASE_URLS = [FUTURES_TESTNET_BASE_URL, FUTURES_DEMO_BASE_URL]

# A felteteles (SL/TP) megbizasok a Binance uj Algo Order API-jan mennek:
# a klasszikus /fapi/v1/order + stopPrice utat az API -4120-cal elutasitja
# ("Please use the Algo Order API endpoints instead").
ALGO_ORDER_ENDPOINT = "/fapi/v1/algoOrder"
OPEN_ALGO_ORDERS_ENDPOINT = "/fapi/v1/openAlgoOrders"

# Ezekre a hibakodokra probaljuk a kovetkezo hostot (rossz/illetektelen kulcs).
_AUTH_ERROR_CODES = ("-2014", "-2015", "-1022")

# Ha az Algo Order API nem elerheto, probaljuk a klasszikus utat.
_ALGO_FALLBACK_CODES = ("-4120", "-5000", "-1102", "-1106")

# A -1111 ("Precision is over the maximum defined for this asset") eseten a
# megbizast egyre durvabb kerekitesel probaljuk ujra.
_PRECISION_ERROR_CODE = "-1111"

# A sajat megbizasainkat errol a prefixrol ismerjuk fel (takaritas / lemondas).
PIVOT_CLIENT_PREFIX = "pivot-"

_CLIENT_ID_SEQ = itertools.count()


class TradingError(RuntimeError):
    """Binance API hiba vagy helyi ellenorzesi hiba."""


# ═════════════════════════════════════════════════════════════
# ADATMODELLEK
# ═════════════════════════════════════════════════════════════

@dataclass
class SymbolFilters:
    tick_size: float
    step_size: float
    min_qty: float
    min_notional: float


@dataclass
class Position:
    amount: float        # >0 long, <0 short, 0 = nincs pozicio
    entry_price: float


@dataclass
class OrderResult:
    order_id: int
    client_order_id: str
    status: str
    executed_qty: float
    avg_price: float
    sent_qty: Optional[str] = None   # a vegso (precizio-kerekites utani) mennyiseg


@dataclass
class LegResult:
    """Egy megbizas-lab eredmenye (a leiras mellett a tenylegesen teljesult mennyiseg)."""
    desc: str
    filled_qty: Decimal
    status: str = ""
    order_id: int = 0
    client_order_id: str = ""


@dataclass
class TradeOutcome:
    """A jelzes vegrehajtasanak osszefoglaloja (+ a piacon maradt limit belepo)."""
    summary: str
    pending: Optional[Dict] = None

    def __str__(self) -> str:
        return self.summary


# ═════════════════════════════════════════════════════════════
# SEGEDFUGGVENYEK
# ═════════════════════════════════════════════════════════════

def _dec(value) -> Decimal:
    return Decimal(str(value))


def _round_step_down(value, step: float) -> Decimal:
    """Mennyiseg lefele kerekitese a LOT_SIZE stepSize-ra (nem kuldhetunk tobbet)."""
    d = _dec(value)
    if not step or step <= 0:
        return d
    s = _dec(step)
    return (d / s).to_integral_value(rounding=ROUND_DOWN) * s


def _round_tick(value, tick: float, rounding=ROUND_HALF_UP) -> Decimal:
    """Ar kerekitese a PRICE_FILTER tickSize-ra."""
    d = _dec(value)
    if not tick or tick <= 0:
        return d
    t = _dec(tick)
    return (d / t).to_integral_value(rounding=rounding) * t


def _round_step_up(value, step: float) -> Decimal:
    """Mennyiseg felfele kerekitese a LOT_SIZE stepSize-ra (a minimum eleresehez)."""
    d = _dec(value)
    if not step or step <= 0:
        return d
    s = _dec(step)
    return (d / s).to_integral_value(rounding=ROUND_CEILING) * s


def _fmt(value: Decimal) -> str:
    """Decimal -> a Binance altal elfogadott, tudomanyos jeloles nelkuli string."""
    return format(value.normalize(), "f")


def _num(value) -> str:
    return f"{float(value):g}"


def _new_client_order_id() -> str:
    return f"{PIVOT_CLIENT_PREFIX}{int(time.time() * 1000)}-{next(_CLIENT_ID_SEQ)}"


def _order_result(data: Dict) -> OrderResult:
    return OrderResult(
        order_id=int(data.get("orderId") or data.get("algoId") or 0),
        client_order_id=str(data.get("clientOrderId") or data.get("clientAlgoId") or ""),
        status=str(data.get("status") or ("NEW" if data.get("success") else "")),
        executed_qty=float(data.get("executedQty", 0) or 0),
        avg_price=float(data.get("avgPrice", 0) or 0),
    )


def _decimals(value: str) -> int:
    return len(value.split(".", 1)[1]) if "." in value else 0


def precision_variants(quantity: Optional[Decimal], price: Optional[Decimal],
                       price_rounding=ROUND_HALF_UP
                       ) -> List[Tuple[Optional[str], Optional[str]]]:
    """
    (mennyiseg, ar) parok egyre kevesebb tizedessel, a kiindulasi ponttal kezdve.

    A -1111 ("Precision is over the maximum defined for this asset") hibat a
    gyakorlatban akkor is adhatja, ha a szimbolum exchangeInfo-ja tobb tizedest
    enged, mint amennyit a matching engine tenylegesen elfogad (a demo/testnet
    konfigja elterhet az elestol - pl. a SOLUSDT-nel 4 helyett 2 tizedes).
    Ezert a visszautasitott megbizast egyre durvabb kerekitesel ujraprobaljuk.
    """
    qty_str = _fmt(quantity) if quantity is not None else None
    price_str = _fmt(price) if price is not None else None
    max_dec = max(_decimals(qty_str or ""), _decimals(price_str or ""))

    variants: List[Tuple[Optional[str], Optional[str]]] = []
    for decimals in range(max_dec, -1, -1):
        factor = 10 ** decimals
        q_str = None
        if quantity is not None:
            q = (quantity * factor).to_integral_value(rounding=ROUND_DOWN) / factor
            if q <= 0:
                continue
            q_str = _fmt(q)
        p_str = None
        if price is not None:
            p = (price * factor).to_integral_value(rounding=price_rounding) / factor
            p_str = _fmt(p)
        pair = (q_str, p_str)
        if pair not in variants:
            variants.append(pair)
    return variants


def _error_text(resp: requests.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:200]
    return f"code={data.get('code')} msg={data.get('msg')}"


def _is_auth_error(exc: "TradingError") -> bool:
    """A kulcsra vonatkozo hiba (pl. -2015 Invalid API-key): ilyenkor mas host kell."""
    text = str(exc)
    return any(code in text for code in _AUTH_ERROR_CODES)


def _futures_permission_hint(text: str) -> str:
    """
    A Binance "The Futures API cannot be used ... IP Access Restriction" hiba
    magyarazata. Ezt a Binance akkor adja, ha a kulcs a Futures szamla megnyitasa
    elott keszult, portfolio margint hasznal a fiok, vagy a kulcson nincs IP-korlat.
    """
    low = text.lower()
    if not ("portfolio margin" in low or "ip access restriction" in low
            or "created before the futures account" in low):
        return ""
    return (
        "\n  -> A Futures API ezzel a kulccsal nem hasznalhato. Lehetseges okok:"
        "\n     1) a kulcs a Futures szamla megnyitasa ELOTT keszult"
        "\n        -> keszits UJ API kulcsot (a szamla megnyitasa utan),"
        "\n     2) a fiokon Portfolio Margin van bekapcsolva"
        "\n        -> kapcsold ki (Derivatives -> Portfolio Margin),"
        "\n     3) a kulcson nincs IP-korlatozas (a Binance uj eloirasa)"
        "\n        -> API Management -> Edit restrictions -> 'Restrict access to"
        " trusted IPs only' + a sajat IP-d, es csak ezutan mentheted az"
        " 'Enable Futures' jogot."
    )


# ═════════════════════════════════════════════════════════════
# BINANCE FUTURES REST KLIENS (HMAC SHA256 alairassal)
# ═════════════════════════════════════════════════════════════

class BinanceFuturesClient:
    """Minimalis USDT-M Futures kliens; csak a kereskedeshez kello vegpontok."""

    def __init__(self, api_key: str, api_secret: str, base_url: str = FUTURES_BASE_URL,
                 timeout: int = 15, session: Optional[requests.Session] = None,
                 fallback_urls: Optional[List[str]] = None, log=print):
        if not api_key or not api_secret:
            raise TradingError(
                "Hianyzik az API kulcs. Eleshez: BINANCE_API_KEY / BINANCE_API_SECRET, "
                "testnet/demo modhoz: BINANCE_DEMO_API_KEY / BINANCE_DEMO_API_SECRET "
                "(lasd .env.example)."
            )
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.fallback_urls = [u.rstrip("/") for u in (fallback_urls or [])]
        self.timeout = timeout
        self.session = session or requests.Session()
        self.log = log
        self._filters_cache: Dict[str, SymbolFilters] = {}

    # ── HTTP ─────────────────────────────────────────────────
    def _request(self, method: str, path: str, params: Optional[Dict] = None,
                 signed: bool = False):
        """
        Alart hivas. Ha a kulcsot az aktualis host elutasitja, es van tartalek
        host (testnet <-> demo), automatikusan atvalt - igy nem kell tudni
        elore, hogy a felhasznalo melyik kornyezetben regisztralt.
        """
        hosts = [self.base_url] + [h for h in self.fallback_urls if h != self.base_url]
        last_error: Optional[TradingError] = None

        for index, host in enumerate(hosts):
            if index:
                self.log(f"[trade] a {hosts[index - 1]} nem fogadta el a kulcsot; "
                         f"proba: {host}")
            try:
                data = self._request_once(host, method, path, params, signed)
            except TradingError as exc:
                last_error = exc
                if signed and index < len(hosts) - 1 and _is_auth_error(exc):
                    continue
                raise
            if host != self.base_url:
                self.base_url = host
                self.log(f"[trade] aktiv futures host: {host}")
            return data

        raise last_error or TradingError("Egyetlen futures host sem volt elerheto.")

    def _request_once(self, base_url: str, method: str, path: str,
                      params: Optional[Dict], signed: bool):
        params = dict(params or {})
        headers = {}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params.setdefault("recvWindow", 5000)
            # A query stringet kell alairni, ugyanabban a sorrendben, ahogy kimegy.
            query = urlencode(params)
            params["signature"] = hmac.new(
                self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            headers["X-MBX-APIKEY"] = self.api_key

        try:
            resp = self.session.request(method, base_url + path, params=params,
                                        headers=headers, timeout=self.timeout)
        except requests.exceptions.RequestException as exc:
            raise TradingError(f"Halozati hiba: {exc}") from exc

        if resp.status_code >= 400:
            if resp.status_code == 451:
                raise TradingError(
                    "451 - a Binance a jelenlegi IP-rol tiltja a Futures API-t. "
                    "Hasznalj engedelyezett orszagbol futo gepet, vagy testnetet (--testnet)."
                )
            detail = _error_text(resp)
            raise TradingError(f"Binance hiba ({resp.status_code}): {detail}"
                               + _futures_permission_hint(detail))

        try:
            return resp.json()
        except ValueError as exc:
            raise TradingError(f"Ervenytelen JSON valasz: {resp.text[:200]}") from exc

    def resolve(self) -> str:
        """
        Egy konnyu, csak olvaso alart hivassal ellenorzi a kulcsot es testnet modban
        kivalasztja azt a hostot, amelyik elfogadja. Visszaadja az aktiv hostot.
        """
        self._request("GET", "/fapi/v2/balance", signed=True)
        return self.base_url

    # ── Piaci adatok / szurok ────────────────────────────────
    def filters(self, symbol: str) -> SymbolFilters:
        if symbol not in self._filters_cache:
            info = self._request("GET", "/fapi/v1/exchangeInfo", {"symbol": symbol})
            symbols = info.get("symbols") or []
            # A futures exchangeInfo a `symbol` parametert figyelmen kivul hagyja
            # (az osszes part visszaadja), ezert kell magunk kiszurni - kulonben
            # minden parra az elso par (BTCUSDT) szuroit hasznalnank.
            match = next((s for s in symbols if s.get("symbol") == symbol), None)
            if match is None:
                raise TradingError(f"Ismeretlen futures par: {symbol}")
            tick_size = step_size = min_qty = min_notional = 0.0
            for flt in match.get("filters", []):
                kind = flt.get("filterType")
                if kind == "PRICE_FILTER":
                    tick_size = float(flt["tickSize"])
                elif kind == "LOT_SIZE":
                    step_size = float(flt["stepSize"])
                    min_qty = float(flt["minQty"])
                elif kind == "MIN_NOTIONAL":
                    min_notional = float(flt.get("notional") or 0)
            self._filters_cache[symbol] = SymbolFilters(tick_size, step_size, min_qty, min_notional)
        return self._filters_cache[symbol]

    def mark_price(self, symbol: str) -> float:
        data = self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
        return float(data["markPrice"])

    # ── Szamla / pozicio ─────────────────────────────────────
    def dual_side_position(self) -> bool:
        """Hedge (dual side) modban van-e a szamla. A vegpont: /fapi/v1/positionSide/dual."""
        data = self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
        return bool(data.get("dualSidePosition"))

    def position(self, symbol: str) -> Position:
        rows = self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
        for row in rows or []:
            if row.get("symbol") == symbol:
                return Position(float(row.get("positionAmt") or 0),
                                float(row.get("entryPrice") or 0))
        return Position(0.0, 0.0)

    def account_summary(self) -> Dict[str, float]:
        data = self._request("GET", "/fapi/v2/account", signed=True)
        return {
            "wallet": float(data.get("totalWalletBalance") or 0),
            "available": float(data.get("availableBalance") or 0),
            "unrealized": float(data.get("totalUnrealizedProfit") or 0),
        }

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self._request("POST", "/fapi/v1/leverage",
                      {"symbol": symbol, "leverage": leverage}, signed=True)

    # ── Megbizasok ───────────────────────────────────────────
    def open_orders(self, symbol: str) -> List[Dict]:
        return self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True)

    def cancel_order(self, symbol: str, client_order_id: Optional[str] = None,
                     order_id: Optional[int] = None) -> None:
        params: Dict = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        else:
            params["origClientOrderId"] = client_order_id
        self._request("DELETE", "/fapi/v1/order", params, signed=True)

    def cancel_pivot_orders(self, symbol: str) -> List[str]:
        """Csak a sajat (pivot- prefixu) nyitott megbizasokat mondja le."""
        cancelled = []
        for order in self.open_orders(symbol):
            cid = order.get("clientOrderId", "")
            if cid.startswith(PIVOT_CLIENT_PREFIX):
                self.cancel_order(symbol, cid)
                cancelled.append(cid)
        for order in self.open_algo_orders(symbol):
            cid = order.get("clientAlgoId", "")
            if cid.startswith(PIVOT_CLIENT_PREFIX):
                self.cancel_algo_order(cid)
                cancelled.append(cid)
        return cancelled

    # ── Algo (felteteles SL/TP) megbizasok ───────────────────
    def place_algo_order(self, symbol: str, side: str, order_type: str, trigger_price: str,
                         close_position: bool = True, quantity: Optional[str] = None,
                         client_algo_id: Optional[str] = None,
                         working_type: Optional[str] = None) -> OrderResult:
        """
        Felteteles megbizas az Algo Order API-n (algoType=CONDITIONAL).

        Fobb elteresek a klasszikus vegponttol: `triggerPrice` (nem stopPrice),
        `clientAlgoId` (nem newClientOrderId). A `closePosition=true` (Close-All)
        triggereleskor a teljes poziciot zarja es a par nyitott megbizasait is
        lemondja.
        """
        params: Dict = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "triggerPrice": trigger_price,
            "newOrderRespType": "RESULT",
        }
        if close_position:
            params["closePosition"] = "true"
        else:
            params["quantity"] = quantity
        if working_type:
            params["workingType"] = working_type
        if client_algo_id:
            params["clientAlgoId"] = client_algo_id
        return _order_result(self._request("POST", ALGO_ORDER_ENDPOINT, params, signed=True))

    def open_algo_orders(self, symbol: str) -> List[Dict]:
        try:
            return self._request("GET", OPEN_ALGO_ORDERS_ENDPOINT, {"symbol": symbol},
                                 signed=True)
        except TradingError as exc:
            # Regebbi kornyezetben nem letezik a vegpont - ez nem hiba.
            self.log(f"[trade] az openAlgoOrders nem elerheto ({exc})")
            return []

    def cancel_algo_order(self, client_algo_id: str) -> None:
        self._request("DELETE", ALGO_ORDER_ENDPOINT,
                      {"clientAlgoId": client_algo_id}, signed=True)

    def place_order(self, symbol: str, side: str, quantity: Optional[str] = None,
                    price: Optional[str] = None, order_type: str = "LIMIT",
                    reduce_only: bool = False, client_order_id: Optional[str] = None,
                    stop_price: Optional[str] = None, close_position: bool = False,
                    working_type: Optional[str] = None) -> OrderResult:
        params = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "newOrderRespType": "RESULT",
        }
        if close_position:
            # Close-All: triggereleskor a teljes pozicio zarul, quantity nem kuldheto.
            params["closePosition"] = "true"
        else:
            params["quantity"] = quantity
            if reduce_only:
                params["reduceOnly"] = "true"
        if order_type == "LIMIT":
            params["timeInForce"] = "GTC"
            params["price"] = price
        if stop_price is not None:
            params["stopPrice"] = stop_price
            if working_type:
                params["workingType"] = working_type
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        return _order_result(self._request("POST", "/fapi/v1/order", params, signed=True))

    def order_status(self, symbol: str, client_order_id: Optional[str] = None,
                     order_id: Optional[int] = None) -> OrderResult:
        params: Dict = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        else:
            params["origClientOrderId"] = client_order_id
        return _order_result(self._request("GET", "/fapi/v1/order", params, signed=True))


def _first_env(*names: str) -> str:
    """Az elso nem ures kornyezeti valtozot adja vissza."""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def build_client(testnet: bool = False, session: Optional[requests.Session] = None,
                 base_url: Optional[str] = None) -> BinanceFuturesClient:
    """
    Kliens a kornyezeti valtozokbol (notify.load_env() tolti a .env fajlbol).

    Host sorrend:
      1. `base_url` (CLI) / `BINANCE_FUTURES_BASE_URL` (env)
      2. testnet/demo modban: testnet.binancefuture.com, majd demo-fapi.binance.com
         (az elso alart hivas valasztja ki, amelyik elfogadja a kulcsot)
      3. egyebkent az eles: fapi.binance.com

    Testnet/demo modban a `BINANCE_DEMO_API_KEY` (vagy `BINANCE_TESTNET_API_KEY`)
    elsobbseget elvez, igy a valodi kulcsot nem kell kivenni a .env-bol.
    """
    env_testnet = os.environ.get("BINANCE_FUTURES_TESTNET", "").strip().lower()
    use_testnet = testnet or env_testnet in ("1", "true", "yes", "on")
    explicit = base_url or os.environ.get("BINANCE_FUTURES_BASE_URL", "").strip() or None

    if use_testnet:
        api_key = _first_env("BINANCE_DEMO_API_KEY", "BINANCE_TESTNET_API_KEY",
                             "BINANCE_API_KEY")
        api_secret = _first_env("BINANCE_DEMO_API_SECRET", "BINANCE_TESTNET_API_SECRET",
                                "BINANCE_API_SECRET")
    else:
        api_key = _first_env("BINANCE_API_KEY")
        api_secret = _first_env("BINANCE_API_SECRET")

    if explicit:
        primary, fallbacks = explicit, []
    elif use_testnet:
        primary, fallbacks = TESTNET_BASE_URLS[0], TESTNET_BASE_URLS[1:]
    else:
        primary, fallbacks = FUTURES_BASE_URL, []

    return BinanceFuturesClient(api_key, api_secret, base_url=primary,
                                fallback_urls=fallbacks, session=session)


# ═════════════════════════════════════════════════════════════
# KERESKEDES
# ═════════════════════════════════════════════════════════════

def limit_price(signal_price: float, side: str, tick_size: float, offset_bps: float) -> Decimal:
    """
    A limit ar a jelzes arahoz kepest. Az `offset_bps` (1 bps = 0.01%) BUY-nal
    felfele, SELL-nel lefele tolja az arat, hogy nagyobb esellyel teljesuljon.

    A tickre is a teljesules iranyaba kerekitunk (BUY fel, SELL le): a testneten
    pl. a SOLUSDT tickje 0.1, ott a "legkozelebbi" tick a belepes elleneben is
    kerekithetne.
    """
    factor = 1.0 + (offset_bps / 10000.0) * (1.0 if side == "BUY" else -1.0)
    rounding = ROUND_UP if side == "BUY" else ROUND_DOWN
    return _round_tick(signal_price * factor, tick_size, rounding)


class FuturesTrader:
    """A jelzeseket Binance Futures megbizasokka forditja (long/short valtas)."""

    def __init__(self, client: BinanceFuturesClient, symbol: str, leverage: int = 1,
                 margin_usdt: float = 10.0, order_type: str = "limit",
                 limit_offset_bps: float = 0.0, order_timeout: float = 20.0,
                 poll_interval: float = 2.0, fallback_market: bool = False,
                 sl_tp: bool = True, dry_run: bool = False, log=print):
        if leverage < 1:
            raise TradingError("A tokeattetel (--leverage) legalabb 1 legyen.")
        if margin_usdt <= 0:
            raise TradingError("A margin (--margin-usdt) nagyobb kell legyen 0-nal.")
        self.client = client
        self.symbol = symbol
        self.leverage = leverage
        self.margin_usdt = margin_usdt
        self.order_type = order_type.upper()
        self.limit_offset_bps = limit_offset_bps
        self.order_timeout = order_timeout
        self.poll_interval = poll_interval
        self.fallback_market = fallback_market
        self.sl_tp = sl_tp
        self.dry_run = dry_run
        self.log = log
        self._leverage_set = False

    # ── Publikus belepesi pont ───────────────────────────────
    def handle_signal(self, kind: str, signal_price: float,
                      sl: Optional[float] = None, tp: Optional[float] = None,
                      hold_until_next_candle: bool = False) -> TradeOutcome:
        """
        Vegrehajtja a jelzest. Visszaadja az emberi nyelvu osszefoglalot.

        `hold_until_next_candle=True` eseten a nyito LIMIT megbizas NEM var a
        teljesulesre es nem is mondjuk le idotullepeskor: a piacon marad a
        kovetkezo gyertya-zarasig, akkor a hivo (pivot_signals) mondja le, ha
        addig nem teljesult.
        """
        kind = kind.upper()
        if kind not in ("BUY", "SELL"):
            raise TradingError(f"Ismeretlen jelzes: {kind}")
        if signal_price <= 0:
            raise TradingError("Ervenytelen jelzes ar.")

        if self.client.dual_side_position():
            raise TradingError(
                "A szamla HEDGE (dual side) modban van. Valts ONE-WAY modra "
                "(Binance -> Preferences -> Position Mode -> One-way Mode)."
            )

        filters = self.client.filters(self.symbol)
        self._ensure_leverage()

        notes: List[str] = []
        position = self.client.position(self.symbol)
        go_long = kind == "BUY"

        if position.amount > 0 and go_long:
            notes.append(f"mar LONG pozicioban ({_num(position.amount)} {self.symbol}) - nincs teendo")
            return TradeOutcome(self._done(notes))
        if position.amount < 0 and not go_long:
            notes.append(f"mar SHORT pozicioban ({_num(position.amount)} {self.symbol}) - nincs teendo")
            return TradeOutcome(self._done(notes))

        # A korabbi (sajat, "pivot-" prefixu) megbizasokat csak akkor mondjuk le,
        # ha tenylegesen zarunk/nyitunk. Kulseben az azonos iranyu jelzesnel a
        # meglevo pozicio SL/TP-je veszne el.
        if not self.dry_run:
            stale = self.client.cancel_pivot_orders(self.symbol)
            if stale:
                notes.append(f"{len(stale)} korabbi nyitott megbizas lemondva")

        if position.amount != 0:
            close_side = "BUY" if position.amount < 0 else "SELL"
            close_qty = _round_step_down(abs(position.amount), filters.step_size)
            if close_qty > 0:
                was = "SHORT" if position.amount < 0 else "LONG"
                notes.append(self._submit(close_side, close_qty, signal_price,
                                          reduce_only=True, label=f"{was} zaras").desc)
            else:
                notes.append("a meglevo pozicio a minimum mennyiseg alatt van - nem zarom")

        open_qty = self._target_quantity(signal_price, filters)
        open_side = "BUY" if go_long else "SELL"
        direction = "LONG" if go_long else "SHORT"
        opened = self._submit(open_side, open_qty, signal_price,
                              reduce_only=False, label=f"{direction} nyitas",
                              wait=not hold_until_next_candle)
        notes.append(opened.desc)

        pending = None
        if hold_until_next_candle and opened.status and opened.status != "FILLED":
            # A limit megbizas a piacon marad. Az SL/TP-t azonnal kirakjuk
            # (Close-All, tehat mennyisegtol fuggetlen), hogy a gyertya kozben
            # teljesulo belepo is vedve legyen - a kovetkezo futas potolja, ha
            # a tozsde pozicio nelkul meg elutasitja.
            if self.sl_tp:
                try:
                    notes.extend(self._place_brackets(go_long, sl, tp, filters))
                except TradingError as exc:
                    # A belepo mar kint van: az allapotot akkor is menteni kell,
                    # hogy a kovetkezo futas le tudja zarni/potolni a SL/TP-t.
                    notes.append(f"SL/TP HIBA: {exc}")
            pending = {
                "side": open_side,
                "signal_price": signal_price,
                "quantity": _fmt(open_qty),
                "sl": sl,
                "tp": tp,
                "status": opened.status,
                "order_id": opened.order_id,
                "client_order_id": opened.client_order_id,
            }
        elif self.sl_tp:
            if opened.filled_qty > 0:
                notes.extend(self._place_brackets(go_long, sl, tp, filters))
            else:
                notes.append("SL/TP kihagyva: a nyito megbizas nem teljesult")
        return TradeOutcome(self._done(notes), pending)

    # ── Belső lepesek ────────────────────────────────────────
    def _done(self, notes: List[str]) -> str:
        text = " | ".join(notes)
        self.log(f"[trade] {text}")
        return text

    def _ensure_leverage(self) -> None:
        if self._leverage_set or self.dry_run:
            return
        self.client.set_leverage(self.symbol, self.leverage)
        self._leverage_set = True

    def _target_quantity(self, price: float, filters: SymbolFilters) -> Decimal:
        """Poziciomennyiseg: margin * tokeattetel / ar, a stepSize-ra lefele kerekitve."""
        notional = self.margin_usdt * self.leverage
        qty = _round_step_down(notional / price, filters.step_size)

        if qty <= 0:
            raise TradingError(
                f"A szamitott mennyiseg 0 {self.symbol} (a par legkisebb lepese "
                f"{filters.step_size:g}). Noveled a --margin-usdt / --leverage erteket "
                f"(most: {self.margin_usdt:g} USDT x {self.leverage}x = {notional:g} USDT)."
            )

        required = self._minimum_quantity(price, filters)
        if qty < required:
            # Az exchangeInfo minimuma szigorubb lehet, mint amit a matching engine
            # tenylegesen elfogad (a demo/testnet konfigja elter az elestol), ezert itt
            # csak figyelmeztetunk - a tozsde a vegso dontes.
            self.log(f"[trade] FIGYELEM: a pozicio ~{float(qty) * price:.2f} USDT, az "
                     f"exchangeInfo minimuma ~{float(required) * price:.2f} USDT - "
                     f"megprobaljuk elkuldeni")
        return qty

    @staticmethod
    def _minimum_quantity(price: float, filters: SymbolFilters) -> Decimal:
        """A legkisebb megengedett mennyiseg: max(minQty, minNotional / ar), stepre kerekitve."""
        step = filters.step_size
        required = _dec(filters.min_qty)
        if filters.min_notional and price > 0:
            required = max(required, _round_step_up(filters.min_notional / price, step))
        required = _round_step_up(required, step)
        # A kerekites miatt meg ellenorizzuk a notional limitet is.
        for _ in range(4):
            if not filters.min_notional or float(required) * price >= filters.min_notional:
                break
            required += _dec(step) if step and step > 0 else _dec(0)
        return required

    def _explain(self, exc: TradingError) -> TradingError:
        """A tozsdei hibak ertheto magyarazattal (a szamokat a sajat beallitasainkbol)."""
        if "-4164" not in str(exc):
            return exc
        return TradingError(
            f"{exc}\n  -> A megbizas erteke a tozsdei minimum (min notional) alatt van. "
            f"Noveld a --margin-usdt / --leverage erteket (most: {self.margin_usdt:g} USDT x "
            f"{self.leverage}x = {self.margin_usdt * self.leverage:g} USDT pozicio)."
        )

    def _place_resilient(self, side: str, quantity: Optional[Decimal], price: Optional[Decimal],
                         reduce_only: bool, order_type: str) -> OrderResult:
        """
        Megbizas kuldese; a -1111 (precizio) hibanál egyre durvabb kerekitesel ujra.

        A visszautasitott megbizas nem ment ki, ezert az ujraproba biztonsagos.
        """
        rounding = ROUND_UP if side == "BUY" else ROUND_DOWN
        variants = precision_variants(quantity, price, rounding)
        last_error: Optional[TradingError] = None

        for index, (qty_str, price_str) in enumerate(variants):
            client_id = _new_client_order_id()
            try:
                res = self.client.place_order(self.symbol, side, qty_str, price=price_str,
                                              order_type=order_type, reduce_only=reduce_only,
                                              client_order_id=client_id)
            except TradingError as exc:
                if _PRECISION_ERROR_CODE not in str(exc):
                    raise self._explain(exc)
                last_error = exc
                continue
            if index:
                self.log(f"[trade] a megbizas precizio-kerekites utan ment ki: "
                         f"qty={qty_str}" + (f" price={price_str}" if price_str else ""))
            if not res.client_order_id:
                # A valasz neha nem tartalmazza - a kesobbi allapotlekerdezeshez
                # (fuggoben maradt limit megbizas) kell.
                res.client_order_id = client_id
            res.sent_qty = qty_str
            return res

        raise self._explain(
            last_error or TradingError("A megbizast egyetlen kerekitessel sem sikerult elkuldeni.")
        )

    def _fill_price(self, res: OrderResult) -> float:
        """
        A teljesult ar. A megbizas-kuldes valasza neha nem tartalmaz avgPrice-t
        (0, illetve clientOrderId-t sem), ilyenkor rakerdezunk a megbizasra.
        """
        if res.avg_price > 0 or not (res.client_order_id or res.order_id):
            return res.avg_price
        try:
            return self.client.order_status(self.symbol, res.client_order_id,
                                            res.order_id or None).avg_price
        except TradingError:
            return res.avg_price

    def _filled_text(self, desc: str, res: OrderResult) -> str:
        price = self._fill_price(res)
        return f"{desc} -> FILLED @ {price:.2f}" if price > 0 else f"{desc} -> FILLED"

    def _submit(self, side: str, quantity: Decimal, signal_price: float,
                reduce_only: bool, label: str, wait: bool = True) -> LegResult:
        filters = self.client.filters(self.symbol)
        qty_str = _fmt(quantity)

        if self.order_type == "MARKET":
            price: Optional[Decimal] = None
            price_str = None
            desc = f"{label}: {side} {qty_str} {self.symbol} MARKET"
        else:
            if signal_price <= 0:
                raise TradingError(
                    f"LIMIT megbizas csak pozitiv arral ertelmezett (kapott: {signal_price})."
                )
            price = limit_price(signal_price, side, filters.tick_size, self.limit_offset_bps)
            price_str = _fmt(price)
            desc = f"{label}: {side} {qty_str} {self.symbol} LIMIT @ {price_str}"
        if reduce_only:
            desc += " (reduceOnly)"

        if self.dry_run:
            return LegResult("DRY-RUN " + desc, quantity)

        res = self._place_resilient(side, quantity, price, reduce_only, self.order_type)
        if res.sent_qty and res.sent_qty != qty_str:
            # A precizio-kerekites miatt tenylegesen elkuldott mennyiseg
            desc = desc.replace(qty_str, res.sent_qty, 1)

        if self.order_type == "MARKET":
            return LegResult(self._filled_text(desc, res), _dec(res.executed_qty),
                             status=res.status, order_id=res.order_id,
                             client_order_id=res.client_order_id)

        filled = _dec(res.executed_qty)
        if res.status == "FILLED" or filled >= quantity:
            return LegResult(self._filled_text(desc, res), filled,
                             status=res.status, order_id=res.order_id,
                             client_order_id=res.client_order_id)

        if not wait:
            # Nem varunk a teljesulesre: a limit megbizas a piacon marad, a
            # hivo donti el, meddig (a pivot_signals a kovetkezo gyertya-zarasig).
            return LegResult(
                f"{desc} -> KIRAKVA a piacon; a kovetkezo gyertya-zarasig ervenyes",
                filled, status=res.status or "NEW", order_id=res.order_id,
                client_order_id=res.client_order_id)

        res = self._wait_for_fill(res)
        filled = _dec(res.executed_qty)

        if res.status == "FILLED" or filled >= quantity:
            return LegResult(self._filled_text(desc, res), filled,
                             status=res.status, order_id=res.order_id,
                             client_order_id=res.client_order_id)

        remaining = quantity - filled
        if res.status in ("NEW", "PARTIALLY_FILLED"):
            try:
                self.client.cancel_order(self.symbol, res.client_order_id or None,
                                         res.order_id or None)
            except TradingError as exc:
                self.log(f"[trade] a megbizas lemondasa nem sikerult: {exc}")

        if remaining <= 0:
            return LegResult(self._filled_text(desc, res), filled,
                             status=res.status, order_id=res.order_id,
                             client_order_id=res.client_order_id)
        if not self.fallback_market:
            return LegResult(
                f"{desc} -> NEM TELJESULT ({res.status}; teljesult: {_num(filled)}"
                f" / {qty_str}) - a maradek nem ment ki", filled,
                status=res.status, order_id=res.order_id,
                client_order_id=res.client_order_id)
        if float(remaining) < filters.min_qty:
            return LegResult(f"{desc} -> reszben teljesult ({_num(filled)}), a maradek a minimum "
                             f"mennyiseg alatt van", filled,
                             status=res.status, order_id=res.order_id,
                             client_order_id=res.client_order_id)

        res2 = self._place_resilient(side, remaining, None, reduce_only, "MARKET")
        filled += _dec(res2.executed_qty)
        price2 = self._fill_price(res2)
        done = f"FILLED @ {price2:.2f}" if price2 > 0 else "FILLED"
        return LegResult(
            f"{desc} -> reszben teljesult, a maradek MARKET-tel: {done}", filled,
            status=res.status, order_id=res.order_id,
            client_order_id=res.client_order_id)

    def ensure_brackets(self, go_long: bool, sl: Optional[float],
                        tp: Optional[float]) -> List[str]:
        """
        SL/TP potlasa a mar meglevo poziciora.

        Arra kell, ha a limit belepo a ket futas kozott teljesult, es a tozsde a
        megbizas kihelyezesekor meg pozicio nelkul (ezert) elutasitotta a bracket-eket.
        """
        return self._place_brackets(go_long, sl, tp, self.client.filters(self.symbol))

    def _place_brackets(self, go_long: bool, sl: Optional[float], tp: Optional[float],
                        filters: SymbolFilters) -> List[str]:
        """
        SL/TP megbizasok a nyitott poziciora (closePosition=true Close-All).

        A stop arat a piaci ar helyes oldalara kell tenni, kulonben a Binance
        -2021-gyel ("would immediately trigger") elutasitja - ezert itt ellenorzunk.
        """
        notes: List[str] = []
        close_side = "SELL" if go_long else "BUY"
        mark = self.client.mark_price(self.symbol)

        for label, order_type, level in (("SL", "STOP_MARKET", sl),
                                         ("TP", "TAKE_PROFIT_MARKET", tp)):
            if level is None or level <= 0:
                notes.append(f"{label}: a jelzesben nincs szint - kihagyva")
                continue
            stop_str = _fmt(_round_tick(level, filters.tick_size))
            # Az SL a veszteseget, a TP a nyereseget zarja: longnal az SL a piaci ar
            # ALATT, a TP FELETTE van; shortnal forditva.
            below_market = go_long if order_type == "STOP_MARKET" else not go_long
            wrong_side = (float(stop_str) >= mark) if below_market else (float(stop_str) <= mark)
            if wrong_side:
                side_text = "alatt" if below_market else "folott"
                notes.append(f"{label} {stop_str} nincs a piaci ar ({mark:.2f}) {side_text} - kihagyva")
                continue
            if self.dry_run:
                notes.append(f"DRY-RUN {label}: {close_side} {self.symbol} {order_type} "
                             f"trigger {stop_str} (closePosition, Algo API)")
                continue
            try:
                self._place_bracket(close_side, order_type, stop_str, label)
            except TradingError as exc:
                notes.append(f"{label} {stop_str} HIBA: {exc}")
                continue
            notes.append(f"{label} beallitva: {stop_str}")
        return notes

    def _place_bracket(self, close_side: str, order_type: str, stop_str: str,
                       label: str) -> OrderResult:
        """
        Egy felteteles lab (SL vagy TP) kuldese.

        Elso ut: Algo Order API (a Binance ide helyezte at a felteteles
        megbizasokat). Ha a kornyezet meg a klasszikus utat varja, arra esunk
        vissza; a -1111 precizio-hibat itt is ujraprobaval kezeljuk.
        """
        client_id = _new_client_order_id()
        last_error: Optional[TradingError] = None

        for trigger in precision_variants(None, _dec(stop_str)):
            stop_value = trigger[1]
            if stop_value is None:
                continue
            try:
                return self.client.place_algo_order(self.symbol, close_side, order_type,
                                                    stop_value, close_position=True,
                                                    client_algo_id=client_id)
            except TradingError as exc:
                text = str(exc)
                if _PRECISION_ERROR_CODE in text:
                    last_error = exc
                    continue
                if not any(code in text for code in _ALGO_FALLBACK_CODES):
                    raise
                self.log(f"[trade] az Algo Order API nem hasznalhato ({exc}); "
                         f"a klasszikus vegponttal probaljuk")
                break

        try:
            return self.client.place_order(self.symbol, close_side, order_type=order_type,
                                           stop_price=stop_str, close_position=True,
                                           client_order_id=client_id)
        except TradingError as exc:
            if last_error is not None:
                raise TradingError(
                    f"{label} nem sikerult. Algo API: {last_error} | klasszikus: {exc}"
                )
            raise

    def _wait_for_fill(self, res: OrderResult) -> OrderResult:
        deadline = time.time() + self.order_timeout
        while res.status in ("NEW", "PARTIALLY_FILLED") and time.time() < deadline:
            time.sleep(self.poll_interval)
            res = self.client.order_status(self.symbol, res.client_order_id)
        return res


# ═════════════════════════════════════════════════════════════
# SZAMLA ALLAPOT (--account)
# ═════════════════════════════════════════════════════════════

def _format_order(order: Dict) -> str:
    text = (f"{order.get('type') or order.get('orderType', '')} "
            f"{order.get('side', '')} "
            f"{order.get('origQty') or order.get('quantity') or ''} "
            f"{order.get('symbol', '')}").strip()
    price = order.get("price")
    if price and float(price) > 0:
        text += f" @ {price}"
    trigger = order.get("stopPrice") or order.get("triggerPrice")
    if trigger and float(trigger) > 0:
        text += f" trigger {trigger}"
    if order.get("closePosition"):
        text += " [closePosition]"
    elif order.get("reduceOnly"):
        text += " [reduceOnly]"
    return text


def print_account(client: BinanceFuturesClient, symbol: str, log=print) -> None:
    client.resolve()
    log(f"Futures host: {client.base_url}")
    summary = client.account_summary()
    log(f"Futures szamla: egyenleg {summary['wallet']:.2f} USDT | "
        f"elerheto {summary['available']:.2f} USDT | "
        f"nem realizalt {summary['unrealized']:+.2f} USDT")

    position = client.position(symbol)
    if position.amount:
        direction = "LONG" if position.amount > 0 else "SHORT"
        log(f"{symbol} pozicio: {direction} {_num(abs(position.amount))} "
            f"@ {_num(position.entry_price)}")
    else:
        log(f"{symbol} pozicio: nincs")

    log("Position mode: " + ("HEDGE (dual side) - a kereskedelemhez ONE-WAY kell"
                             if client.dual_side_position() else "ONE-WAY"))

    filters = client.filters(symbol)
    log(f"Mark ar: {client.mark_price(symbol):.2f} | tick {filters.tick_size:g} | "
        f"step {filters.step_size:g} | min qty {filters.min_qty:g} | "
        f"min notional {filters.min_notional:g} USDT")

    orders = client.open_orders(symbol)
    algo_orders = client.open_algo_orders(symbol)
    if orders or algo_orders:
        log(f"Nyitott megbizasok ({len(orders) + len(algo_orders)}):")
        for order in orders:
            log("  " + _format_order(order))
        for order in algo_orders:
            log("  " + _format_order(order) + " [algo]")
    else:
        log("Nyitott megbizas: nincs")
