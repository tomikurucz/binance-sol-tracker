# SOL Pivot Signals – ingyenes futtatás GitHub Actions-szel

Binance SOL 1h pivot-breakout jelzések + push értesítés a telefonra.
Szerver nélkül, ingyen: a GitHub Actions óránként lefuttatja a szkriptet.

## Fájlok

| Fájl | Szerep |
|---|---|
| `sol_tracker.py` | Egyszerű árfolyam-figyelő másodpercenként |
| `pivot_signals.py` | A Pine Script portja (volume, fordulat, pivot és follow-through jelzések + SL/TP) + értesítés (+ opcionális megbízás-küldés) |
| `trader.py` | Binance USDT-M Futures megbízások (long/short váltás, SL/TP bracket, aláírt REST) |
| `notify.py` | Push küldés (ntfy / Telegram / Discord / Pushover) |
| `.env.example` | Értesítési beállítások sablonja |
| `.github/workflows/signals.yml` | Az óránkénti futtatás (GitHub Actions) |

## 1. Értesítés beállítása

Válassz egy csatornát. A legegyszerűbb az **ntfy** (ingyenes, nem kell regisztráció):

1. Telepítsd a **ntfy** appot a telefonra
2. Válassz egy egyedi, nehezen kitalálható topic nevet, pl. `sol-pivot-a1b2c3d4`
3. Az appban: **Subscribe** → add meg ugyanezt a topic nevet
4. Helyben teszteld:

```powershell
python pivot_signals.py --test-notify
```

> Éles módban (`--live --notify`) a szkript **induláskor azonnal** küld egy
> "figyelés elindult" értesítést, így rögtön látod, hogy él a kapcsolat.

### Telegram (push a telefonra)

1. Telegramban keresd meg a **@BotFather** botot → `/newbot` → adj neki nevet és
   felhasználónevet (`..._bot` végződéssel) → megkapod a **tokent**
   (pl. `123456789:AAF...`).
2. **Írj a saját botodnak egy üzenetet** (pl. `/start`) – enélkül a bot nem lát
   chatet, és nem tud üzenetet küldeni.
3. Kérd le a `chat_id`-t a beépített segéddel:

```powershell
python pivot_signals.py --telegram-chat-id 123456789:AAF...
```

4. Írd be a `.env`-be:

```ini
TELEGRAM_BOT_TOKEN=123456789:AAF...
TELEGRAM_CHAT_ID=123456789
```

5. Teszteld:

```powershell
python pivot_signals.py --test-notify
```

Ez kiírja: `Beallitott csatornak: ntfy, telegram` – ha mindkettő szerepel, mindkét
helyre megy a jelzés. Ha a bot nem talál chatet, írj neki még egy üzenetet
(és botnál a `/start` gombot is nyomd meg), majd futtasd újra a 3. lépést.

> A jelzések címe félkövérrel megy ki (Markdown); ha a szöveg mégis olyan
> karaktert tartalmazna, amit a Telegram nem tud értelmezni, a szkript
> automatikusan sima szövegként küldi újra.

## 2. Feltöltés a GitHubra

1. A [github.com](https://github.com)-on **New repository** → név: `binance-sol-tracker`
2. A projektmappában:

```powershell
cd C:\Users\tomik\binance-sol-tracker
git init
git add .
git commit -m "SOL pivot signals + notifications"
git branch -M main
git remote add origin https://github.com/<FELHASZNALONEV>/binance-sol-tracker.git
git push -u origin main
```

> A `.gitignore` gondoskodik róla, hogy a `.env` (titkok!) **ne** kerüljön fel.

## 3. Titkok beállítása

A repóban: **Settings → Secrets and variables → Actions → New repository secret**

Add hozzá, amelyiket használod:

| Secret | Érték |
|---|---|
| `NTFY_TOPIC` | a topic neved (csak ide, secretbe – ne írd a repóba!) |
| `TELEGRAM_BOT_TOKEN` | *(ha Telegramot használsz)* |
| `TELEGRAM_CHAT_ID` | *(ha Telegramot használsz)* |
| `DISCORD_WEBHOOK_URL` | *(ha Discordot használsz)* |

## 4. Ellenőrzés

**Actions** fül → `Pivot signals -> push` → **Run workflow** (kézi indítás).

Ezután magától fut minden órában, **az órafordulóra időzítve**: a workflow
`:58`-kor indul, és a job belül megvárja a következő egész órát (+3 mp), hogy
pontosan a frissen lezárt 1h gyertyát értékelje. Ha a GitHub cron késett, akkor
várakozás nélkül, azonnal értékel — a gyertya ekkor is a helyes, lezárt gyertya.
Értesítés csak akkor megy, ha ezen a gyertyán jelzés keletkezett.

> **Teszt értesítés a GitHubról:** a *Run workflow* panelen pipáld be a
> **„Csak teszt értesítés küldése"** opciót → így azonnal kapsz egy teszt push-t a
> telefonodra (ilyenkor nem fut jelzés-ellenőrzés és nem módosul az állapot).
> A sima (kijelöletlen) futtatás csak akkor küld, ha tényleg van új jelzés.

### Ráfutás a gyertya-zárás előtt (`--intrabar`)

Opcionális előzetes értesítés: a `--intrabar` (alias `--pre-close`) a **még
nyitott** gyertyát értékeli a zárás előtti utolsó percekben, és **csak a
kék/narancs** (volume / fordulat) jelzésekről küld push-t – ezek nem
kereskednek, tehát itt nincs késleltetett megbízás. A zöld/piros (pivot és
follow) jelzésekről nem küld előzetest, mert azokra a zárás után valódi
megbízás megy. A ráfutás nem
kereskedik, és a gyertyát nem „fogyasztja el”: a zárás utáni rendes futás
ugyanazt a jelzést **ismét elküldi**. A `last_signal.json` `intrabar` kulcsa
csak azt akadályozza meg, hogy a ráfutás ugyanarra a (még nyitott) gyertyára
többször küldjön.

Helyi Feladatütemezővel ehhez egy második feladat kell (pl. `:55`-kor):

```
run_once.cmd --notify-once --symbol SOLUSDC --margin-usdt 2 --leverage 20 --intrabar
```

- `--intrabar-minutes` (alap: 5): hány perccel a zárás előtt értékeljen. Ha a
  feladat korábban indul, a szkript „most nem a zárás előtti … percben járunk”
  üzenettel kilép.
- A gyertya adatai ilyenkor még **hiányosak** (pl. `:55`-kor a végső volumen
  ~92%-a van meg), ezért ez előzetes jelzés: a zárás után véglegesülhet.
- Csak a kék/narancs jelzések mennek ki; a zöld/piros pivot jelzéseket és a
  kereskedést a zárás utáni futás intézi.

## 5. Binance Futures megbízások (élő kereskedés)

A jelzésekből valódi Binance **USDT-M Futures** megbízásokat a `--trade` flag
küldi. Enélkül a szkript csak jelez/értesít – semmit nem kereskedik.

### Mit csinál egy jelzés?

| Jelzés | Művelet |
|---|---|
| **zöld BUY** (pivot) | a meglévő SHORT zárása (reduceOnly), majd LONG limit a záró áron + SL/TP |
| **piros SELL** (pivot) | a meglévő LONG zárása (reduceOnly), majd SHORT limit a záró áron + SL/TP |
| **zöld BUY / piros SELL** (követő, follow) | ugyanez: zárás + LONG/SHORT limit a záró áron + a follow SL/TP-vel |
| **kék / narancs** (volume, fordulat) és **éjszakai fekete** | **csak értesítés** – semmilyen megbízás nem megy ki |

A kereskedés tehát a **címke színe** alapján dől el: zöld/piros = megbízás,
minden más (kék, narancs, fekete) = csak értesítés. A pivot kanócos (rózsaszín)
jelzése is pivot, ezért az is kereskedett.

A nyitó limit megbízás a **következő órás gyertya zárásáig** marad a piacon; ha
addig nem teljesül, akkor mondja le a szkript (a hozzá tartozó SL/TP-vel együtt).

- Ha **már a jelzés irányában** áll a pozíció, nem történik semmi (nincs
  véletlen tőkeáttétel-növelés), és a meglévő SL/TP **érintetlenül marad**.
- A záró láb mindig `reduceOnly`, így nem fordulhat át véletlenül nyitó
  megbízásba.
- A szkript a **saját** (`pivot-` prefixű) korábbi, nyitva maradt megbízásait
  csak akkor mondja le, amikor tényleg zár/nyit (a régi SL/TP-t is) – a kézzel
  felvett megbízásaidat nem bántja, és azonos irányú jelzésnél nem hagyja
  fedezetlenül a pozíciót. Kivétel a **lejárt limit belépő**: azt a következő
  gyertya-zárásnál mindenképp lemondja (a hozzá tartozó SL/TP-vel együtt).
- A számla **one-way** (egyirányú) pozíciómódban kell legyen; hedge módban a
  szkript hibát ír és nem kereskedik.

### Stop-loss és take-profit (a Pine-nal egyezően)

A szintek a jelzés gyertyáján számolódnak, pontosan úgy, ahogy a Pine Scriptben:

| Jelzés | SL | TP |
|---|---|---|
| **BUY** | `ta.lowest(low, slLookback)` – az utolsó *N* gyertya legalacsonyabb low-ja | `lastPivotHigh` (utolsó megerősített pivot high) |
| **SELL** | `ta.highest(high, slLookback)` – az utolsó *N* gyertya legmagasabb high-ja | `lastPivotLow` (utolsó megerősített pivot low) |

Az *N* a `--sl-lookback` (alapérték **5**, a Pine „SL Lookback” inputnak felel
meg). A szintek a jelzés-értesítésben és a `--dry-run` kimenetében is megjelennek.

> A **követő (follow)** jelzésekhez a Pine a charton szintén húz SL/TP vonalat
> (SL = a jelzett gyertya low/high, TP = a legközelebbi **még nem érintett**
> pivot, ha nincs ilyen: 2×ATR). Mivel a zöld/piros follow jelzés is kereskedett,
> ezek a szintek a pivot szintekkel azonos módon **megbízásba mennek**
> (STOP_MARKET / TAKE_PROFIT_MARKET, `closePosition=true`).

A Binance-en a nyitó limit megbízás kihelyezésekor (a teljesülésére nem várva)
két **feltételes** megbízás kerül fel a pozícióra:

```
STOP_MARKET          triggerPrice = SL   closePosition=true   (SL találatkor zár)
TAKE_PROFIT_MARKET   triggerPrice = TP   closePosition=true   (TP találatkor zár)
```

- Ezek a **Binance Algo Order API-ján** mennek (`POST /fapi/v1/algoOrder`,
  `algoType=CONDITIONAL`), mert a klasszikus `/fapi/v1/order` végpont a
  feltételes típusokat már elutasítja
  (*-4120: „Order type not supported for this endpoint. Please use the Algo Order
  API endpoints instead"*). Ha egy környezet mégis a régi utat várja, a szkript
  automatikusan visszaesik rá.
- Az Algo API-n a trigger ár `triggerPrice` (a régi `stopPrice` helyett), a saját
  azonosító pedig `clientAlgoId` (`pivot-…`) – a listázásuk
  (`GET /fapi/v1/openAlgoOrders`) és a lemondásuk (`DELETE /fapi/v1/algoOrder`)
  külön végponton megy, ezt a szkript kezeli.
- A `closePosition=true` (Close-All) triggereléskor a **teljes pozíció** zárul, és
  a Binance a pár többi nyitott megbízását is lemondja – így nem marad árva SL a
  TP teljesülése után.
- A trigger a **last price** alapján történik (Binance `workingType`
  alapérték), ami a Pine gyertya high/low logikájának felel meg.
- Ha egy szint a piaci ár **rossz oldalán** van (pl. a TP az entry alatt), a
  Binance azonnal triggerelné, ezért a szkript azt a lábat kihagyja és
  figyelmeztet – ezt a 3000 gyertyás visszatesztben egyetlen jelzésnél sem
  tapasztaltuk.
- **Precízió (`-1111`):** a Binance akkor is adhat „Precision is over the
  maximum" hibát, ha a szimbólum `exchangeInfo`-ja több tizedest enged, mint
  amennyit a matching engine ténylegesen elfogad (a demón pl. a SOLUSDT 4 helyett
  2 tizedest fogad el). A szkript ilyenkor egyre durvább kerekítéssel újrapróbálja
  – ez biztonságos, mert a visszautasított megbízás nem ment ki.
- Ha nem kell SL/TP, `--no-sl-tp`-vel kikapcsolható.
- Ellenőrzés: `python pivot_signals.py --account` kiírja a nyitott
  megbízásokat is (`STOP_MARKET SELL 0.96 SOLUSDT trigger 102.8 [closePosition] [algo]`).

### Első lépések (mindig testnettel kezdj!)

**1. Éles API kulcs** – Binance → profil → **API Management** → *Create API*. Jogok:

- ✅ **Enable Futures**
- ❌ semmi más (főleg **nem** Withdraw!)
- javasolt: **Restrict access to trusted IPs only**

**2. `.env` kitöltése**

```ini
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
```

> **Ha ezt írja:** *„The Futures API cannot be used if the API key was created
> before the Futures account was opened, or if you have enabled portfolio margin.
> You must apply the IP Access Restriction filter in order to enable this
> permission.”* – ez a Binance válasza (jellemzően `-2015`), és három oka lehet:
>
> 1. **a kulcs a Futures számla megnyitása előtt készült** → készíts **új** API
>    kulcsot (most, a számla megnyitása után);
> 2. **Portfolio Margin** van bekapcsolva a fiókon → kapcsold ki
>    (*Derivatives → Portfolio Margin*), vagy használj másik fiókot;
> 3. **nincs IP-korlátozás a kulcson** → az *API Management → Edit restrictions*
>    alatt állítsd be: **Restrict access to trusted IPs only** + a saját IP-d
>    (`curl https://ifconfig.me`), és **csak ezután** menthető az *Enable Futures*.
>
> A demó/testnet kulcs külön rendszer, ott ez a megkötés nem él – ezért működött
> a teszt. Mivel a kulcs IP-hez kötött, **GitHub Actions-ből nem lehet élesben
> kereskedni** (a runner IP-je folyton változik): ehhez fix IP-es gép/VPS kell.

**3. Ellenőrzés / száraz próba** (nem küld megbízást):

```powershell
python pivot_signals.py --account                        # egyenleg, pozíció, szűrők, position mode
python pivot_signals.py --trade --dry-run --notify-once   # mit küldene a legutóbbi jelzésre
```

**3b. Önteszt – a megbízások és az SL/TP ellenőrzése valódi megbízásokkal**

Ez a teszt ugyanazt csinálja, mint egy jelzés: nyit egy kis pozíciót SL/TP-vel,
ellenőrzi, hogy mindkét láb megjelent-e a megfelelő oldalon, majd lemondja a
megbízásokat és lezárja a pozíciót. A végén a számla **lapos** (nincs pozíció,
nincs nyitott megbízás), a csak a spread/díj különbség jelenik meg.

```powershell
python pivot_signals.py --test-trade --testnet --margin-usdt 6 --leverage 10   # demón
python pivot_signals.py --test-trade --margin-usdt 6 --leverage 1              # ÉLES, ~6 USDT
```

Kimenet (minden ellenőrzés után összegzés és exit kód; hiba esetén `exit 1`):

```
[teszt] Eredmenyek:
  [OK ] pozicio nyitva: +0.5600 SOLUSDT
  [OK ] SL megbizas: STOP_MARKET
  [OK ] TP megbizas: TAKE_PROFIT_MARKET
  [OK ] SL/TP oldala (SELL): SELL
  [OK ] pozicio lezarva: +0.0000
  [OK ] nincs nyitott megbizas: 0 algo / 0 sima
[teszt] MINDEN ELLENORZES OK - a megbizasok es az SL/TP is mukodik.
```

Ha már van nyitott pozíció, a teszt **nem indul el** (nehogy véletlenül bezárja);
`--force`-fal viszont lefut (a végén lapos állapotban hagyja a számlát). A
`--dry-run`-nal nem kombinálható, mert ez valódi megbízásokat küld.

**4. Éles használat (valódi pénz):**

```powershell
python pivot_signals.py --trade --live                                  # alap: 10 USDT margin, 1x
python pivot_signals.py --trade --live --margin-usdt 20 --leverage 3    # 20 USDT margin, 3x
```

> A `--trade` / `--dry-run` **csak** a `--live` vagy a `--notify-once` móddal együtt
> értelmezett (a `--live` a másodpercenkénti figyelés, a `--notify-once` az egyszeri
> kiértékelés cron/CI-hez).

### Testnet / Demo Trading – így tesztelj ingyen

A Binance a régi **Futures Testnet** felületet beolvasztotta a **Demo Trading**be:
a `testnet.binancefuture.com` minden webes útvonala (regisztráció, API kezelés is)
átirányít a `demo.binance.com`-ra – **ott már nem lehet kulcsot készíteni**. Az API
végpont viszont továbbra is él (a régi kulcsokhoz), és a **piaci adat ugyanaz**
mindkét hoston (ellenőrizve: azonos gyertyák).

| Környezet | API host | Kulcsot hol készíted |
|---|---|---|
| **Demo Trading** (ajánlott) | `https://demo-fapi.binance.com` | [binance.com → Demo Trading](https://www.binance.com/en/demo-trading) → **API Management** (demo módban) |
| Régi Futures Testnet | `https://testnet.binancefuture.com` | ❌ már nem elérhető (csak a korábban készített kulcsok működnek) |

A szkript **automatikusan kiválasztja** azt a hostot, amelyik elfogadja a kulcsodat
(az első aláírt hívás dönti el, és kiírja az aktív hostot), így nem kell tudnod
előre, melyik környezetben regisztráltál.

**1. Demo kulcs a `.env`-be** (a valódi kulcsot a demo elutasítja, ezért külön
mezőben van – a kettő egymás mellett is megfér):

```ini
BINANCE_DEMO_API_KEY=...
BINANCE_DEMO_API_SECRET=...
```

> A demo számlát a **valódi Binance fiókoddal** éred el (nincs külön regisztráció),
> és virtuális egyenleget kapsz. A kulcson csak **Enable Futures** jog kell,
> `Withdraw` soha. A demo környezet időnként **resetelődik** (egyenleg, pozíciók,
> kulcsok eltűnhetnek).

**2. Kapcsolat ellenőrzése** – kiírja az aktív hostot és a virtuális egyenleget:

```powershell
python pivot_signals.py --account --testnet
```

**3. Száraz próba testneten** (nem küld semmit):

```powershell
python pivot_signals.py --trade --dry-run --notify-once --testnet
```

**4. Éles teszt testneten** (valódi megbízások a virtuális számlán):

```powershell
python pivot_signals.py --trade --testnet --live --margin-usdt 1 --leverage 20
```

> **A demo/testnet szűrők eltérnek az élesétől!** A demó SOLUSDT-nál mért értékek:
> `tick 0.1`, max tőkeáttétel `50x`, `exchangeInfo` szerinti
> `min notional 50 USDT` – miközben maga a matching engine **2 tizedes** mennyiséget
> és **5 USDT** minimumot alkalmaz. A szkript az `exchangeInfo`-ot használja a
> méretezéshez, de ha a pozíció annak a minimuma alatt van, csak **figyelmeztet és
> elküldi** (a tőzsde a döntőbíró), a `-1111`/`-4164` hibákat pedig
> kerekítés-újrapróbával, illetve érthető magyarázattal kezeli. Így a kis pozíciók is
> működnek: a **1 USDT margin × 20x = 20 USDT** (≈0,19 SOL) élesben végigfutott.
> A demo számla alapból ~5000 virtuális USDT-vel indul.

#### Reakcióidő (mért értékek)

A `--live` a gyertya zárására igazodik, és 150 ms-os felbontással figyeli, mikor
jelenik meg az új gyertya. Mért publikációs késleltetés (2026-10-16 19:00 UTC):

| Adatforrás | A lezárt gyertya megjelenése |
|---|---|
| `data-api.binance.vision` (mainnet, alapértelmezett) | **+1,99 s** |
| `testnet.binancefuture.com` (demo) | **+9,05 s** (a szimulált piac lassabban generál) |

A lezárt gyertya adatai a megjelenés után már **nem változnak** (ellenőrizve), tehát
a jelzés végleges. Így a tipikus reakció élesben **~2,2 s** a zárás után, testneten
~9,2 s.

Összehasonlításul: a helyi Feladatütemezőből `:00:20`-kor indított `--notify-once`
~20 s-mal a zárás után értékel. Egy 1 órás stratégiánál ez a ~18 s különbség
gyakorlatilag elhanyagolható, cserébe a `--live`-nak **folyamatosan futnia kell**.

#### Amit a testnetről tudni kell

- A `--testnet` a **kereskedést és a piaci adatot is** a testnetre váltja, hogy a
  jelzés, az SL/TP szintek és a limit ár ugyanabból a piacból származzanak, ahol a
  megbízás megy. A mainnet és a demo/testnet gyertyák gyakorlatilag egyeznek
  (az eltérés jellemzően <0,1%), így a `--mainnet-data` is használható; ha a
  limit mégsem teljesülne, `--order-type market` vagy `--limit-offset-bps` segít.
- A testnet piaca **vékonyabb és szeszélyesebb**, mint az éles: előfordulhat, hogy
  egy szint a piaci ár „rossz” oldalára kerül – ilyenkor a szkript azt a lábat
  kihagyja és figyelmeztet (éles adaton 205 jelzésből 0 ilyen volt, testneten 25-ből 1).
- A testnet hostokra **nem kell** a valódi Binance API-tól eltérő kód: csak a
  `--testnet` (vagy `BINANCE_FUTURES_TESTNET=1`) kell.
- Az **elavult jelzések** nem kereskednek: `--trade-max-age` (alapérték `1` gyertya)
  óvja meg attól, hogy egy újraindítás után a régi jelzéseket elavult áron küldje
  ki. A határérték **bezárólag** értendő: `1` esetén az egy gyertyával ezelőtt
  zárt jelzés is kereskedhető (a pivot visszaigazolás tipikusan ilyen), és a
  gyertya-zárás utáni pár másodperces késés is belefér. Ha kimaradt jelzéseket is
  kereskedni akarsz, emeld meg (pl. `--trade-max-age 5`), vagy `0` = nincs korlát.
- **Tiszta lappal induláshoz** töröld a `last_signal.json`-t, különben a `--live`
  első köre a fájlban tárolt időpont utáni jelzéseket próbálja „pótolni”
  (kereskedni ezekkel a `--trade-max-age` miatt már nem fog).
- A `--dry-run` **nem** írja a `last_signal.json`-t, így egy előnézet nem
  „fogyasztja el” a jelzést – a következő éles futás ugyanazt a jelzést még
  kereskedni fogja.
- A piacon maradt limit belépő nyilvántartása a `pending_trade.json`-ban van. Ha
  ezt futás közben törlöd, a szkript nem fogja tudni lemondani a megbízást – azt
  a tőzsdén kézzel kell.

### Paraméterek

| Flag | Alapérték | Jelentés |
|---|---|---|
| `--trade` | ki | valódi megbízások küldése (csak `--live`/`--notify-once` mellett) |
| `--dry-run` | ki | csak kiírja a tervet, nem küld semmit |
| `--testnet` | ki | testnet/demo: kereskedés **és** piaci adat a testnetről |
| `--mainnet-data` | ki | testnet kereskedés mellett mainnet gyertyák |
| `--futures-url` | – | kézzel megadott futures host (pl. `https://demo-fapi.binance.com`) |
| `--margin-usdt` | `10` | egy pozícióra szánt margin (USDT) |
| `--leverage` | `1` | tőkeáttétel (a pozíció mérete = margin × leverage) |
| `--order-type` | `limit` | `limit` vagy `market` |
| `--limit-offset-bps` | `0` | a limit ár eltolása a jelzés árától (1 bps = 0,01%) |
| `--order-timeout` | `20` | ennyi mp-ig vár a **záró** láb teljesülésére (a nyitó limit a következő gyertya-zárásig él) |
| `--fallback-market` | ki | a záró láb időtúllépésekor a maradék megy MARKET-tel |
| `--no-sl-tp` | ki | ne tegyen SL/TP megbízást a pozícióra |
| `--trade-max-age` | `1` | ennyi gyertyán belül zárt jelzésre kereskedjen, a határt beleértve (0 = nincs korlát) |
| `--sl-lookback` | `5` | SL-hez használt gyertyaszám (Pine „SL Lookback”) |
| `--volume-multiplier` | `2` | volume spike küszöb (Pine „N-szeres mennyiség”) |
| `--volume-lookback` | `10` | volume átlagolási periódus |
| `--volume-ma` | `sma` | `sma` / `wma` / `ema` (a Pine MA típusa) |
| `--reversal-lookback` | `5` | fordulat: visszatekintő gyertyák |
| `--reversal-mode` | `strict` | `strict` = mind, `majority` = többség |
| `--follow-bars` | `1` | SL/TP után hány gyertyán belül vár follow zárást (a Pine-ban 1 = csak a következő gyertya) |
| `--wick-ratio` | `2.0` | kanóc arány a rózsaszín pivot jelzéshez |
| `--show-pink-signal` | ki | rózsaszín (kanócos) pivot címkék (Pine „Rózsaszín pivot jelzés megjelenítése”) |
| `--pivot-levels` | ki | pivot high/low szintcímkék a grafikonon (Pine „Pivot szintek megjelenítése”) |
| `--no-follow-labels` | ki | a követő (follow) BUY/SELL címkék elrejtése a grafikonon |
| `--night-start` / `--night-end` | `0` / `7` | éjszakai ablak (Europe/Budapest óra) |
| `--label-offset` | `0.5` | BUY/SELL és SPIKE címke távolsága ATR-ben |
| `--sl-line-length` | `10` | SL/TP vonal hossza gyertyában |
| `--no-spike-label` | ki | ne rajzolja a sárga **SPIKE** címkéket a grafikonra |
| `--no-spike-details` | ki | a SPIKE címke csak a szót mutassa (mennyiség és ×arány nélkül) |
| `--spike-label-color` | sárga | SPIKE címke háttere |
| `--spike-label-text-color` | fekete | SPIKE címke szövegszíne |
| `--spike-label-size` | `normal` | `tiny` / `small` / `normal` / `large` / `huge` |
| `--spike-alert` | ki | push értesítés minden volume spike-ról (a jelzésektől függetlenül) |
| `--intrabar` | ki | ráfutás: a **még nyitott** gyertyát értékeli, csak kék/narancs jelzésre küld (csak `--notify-once` mellett) |
| `--intrabar-minutes` | `5` | a zárás előtti ennyi percen belül értékel a `--intrabar` |
| `--account` | – | számla állapot + nyitott megbízások kiírása és kilépés |
| `--test-trade` | ki | **önteszt**: nyit egy kis pozíciót SL/TP-vel, ellenőrzi, majd zárja (a végén lapos a számla) |
| `--force` | ki | a `--test-trade` akkor is fusson, ha van nyitott pozíció (azt is lezárja) |

A pozíció mérete: `margin × leverage / ár`, a `stepSize`-ra **lefelé** kerekítve,
és ellenőrzi a `minQty` / `minNotional` limiteket is.

> **20x példa:** `--margin-usdt 1 --leverage 20` → 20 USDT pozíció (≈0,19 SOL),
> ~103,8-es áron a likvidációs ár ~5%-nyira van. A SOLUSDT maximuma 50x.
> Ha a számított mennyiség **0-ra kerekedik**, a szkript érthető hibát ír
> („növeld a --margin-usdt / --leverage értéket”); ha csak a minimum alatt van,
> figyelmeztet és megpróbálja elküldeni.

> **SOLUSDT példa:** a szűrők `stepSize 0.01 SOL`, `minNotional 5 USDT`.
> `--margin-usdt 10 --leverage 1` → ~0,05 SOL ≈ 10 USDT pozíció (rendben).
> `--margin-usdt 0.2` → 0,00 SOL, ezért a szkript leáll:
> *„legalább 0.03 SOLUSDT (~5.96 USDT) kell. Ez 1x tőkeáttételnél ~5.96 USDT
> margint igényel: --margin-usdt 6"*.

### Limit vs. market

Alapértelmezésben **limit** megbízás megy a jelzés gyertyájának záró árán, és a
piacon **a következő órás gyertya zárásáig** marad:

- Ha addig teljesül, a pozíció megnyílik. A SL/TP (Close-All) megbízások már a
  belépővel együtt kikerülnek, így a gyertya közbeni teljesülés is védett; ha a
  tőzsde pozíció nélkül elutasítja őket, a következő futás pótolja.
- Ha a következő gyertya zárásakor sem teljesült, a szkript lemondja (a hozzá
  tartozó, esetleg ott maradt SL/TP megbízásokkal együtt), és az új jelzéssel
  indul tovább. Az állapot a `pending_trade.json`-ban van.
- A nyitó limitet ezért **nem** érinti az `--order-timeout` / `--fallback-market`
  – ezek csak a záró (reduceOnly) lábra vonatkoznak.
- `--limit-offset-bps 5` – a BUY limit 0,05%-kal a jelzés ár**a fölé**, a SELL
  0,05%-kal **alá** kerül → nagyobb eséllyel teljesül.

### Értesítés

Ha a `--notify` is be van kapcsolva, a push üzenet tartalmazza a jelzés SL/TP
szintjeit, és a végén a kereskedés eredményét is (pl. `[Trade] LONG nyitás: BUY
0.05 SOLUSDT LIMIT @ 104.04 -> FILLED | SL beallitva: 103.53 | TP beallitva:
106.51`). Ha a limit belépő a piacon marad, a sor `KIRAKVA a piacon; a
következő gyertya-zárásig érvényes` végződésű. A `[Trade]` sor akkor is
megjelenik, ha a kereskedést kihagyta (elavult jelzés) vagy hiba történt.

Ha nincs új jelzés, **nem** megy értesítés – a futás csendben lezárul
(`Mar feldolgozva` / `Nincs uj jelzes`). A pozíció **lezárásáról** (SL/TP
teljesülése) szintén nincs üzenet, mert az a tőzsdén történik, a szkriptet nem
értesíti róla: az eredményt (`--account`) vagy a naplót kell megnézni.

> ⚠️ **Kockázat:** az SL/TP megbízások a Binance oldalán feltételes
> megbízásokként élnek, tehát akkor is zárnak, ha a szkript nem fut. Ha a
> `--no-sl-tp`-t használod (vagy a jelzésből hiányzik egy szint), a pozíció
> fedezetlen marad a következő ellenkező irányú jelzésig. Tőkeáttételnél a
> veszteség meghaladhatja a betett margint. Éles használat előtt teszteld
> testneten, és használj akkora margint, amit el tudsz veszíteni.

> **GitHub Actions:** a workflow-ból is lehet kereskedni (a Binance kulcsokat
> repository secretként add hozzá, és a `run:` sorba vedd fel a `--trade` flaget),
> de ekkor **IP-szűrést nem tudsz** használni, mert a runner IP-je folyton
> változik. Éles kereskedéshez ezért always-on gépen futtatott
> `--live --trade` mód ajánlott.

## 6. Saját szerver (VPS): ütemezés

Éles, fix IP-s futtatás: a bot óránként, a gyertya-zárás után pár másodperccel
lefut egyszer (`--notify-once --trade`), a GitHub Actions helyett. A fix IP-t a
szolgáltató adja, ezt kell a Binance API kulcs whitelistjére felvenni.

Egyszeri telepítés a repó gyökeréből (a `deploy/` mappa tartalma):

```bash
sudo bash deploy/install-scheduler.sh
```

Ez a következőket teszi:

| Fájl | Hely | Szerep |
|---|---|---|
| `deploy/sol-tracker.service` | `/etc/systemd/system/` | egy futás: `pivot_signals.py --symbol SOLUSDC --margin-usdt 2 --leverage 20 --trade --notify-once` |
| `deploy/sol-tracker.timer` | `/etc/systemd/system/` | óránként `:02`-kor (szerver órája szerint – állítsd UTC-re) |
| `deploy/logrotate.conf` | `/etc/logrotate.d/sol-tracker` | heti forgatás a `logs/signals.log`-ra |

A szkript a saját útvonalát és a `sudo`-t indító felhasználót írja be a unitba,
tehát bármelyik mappába klónozva működik (más felhasználóhoz:
`sudo SOL_TRACKER_USER=bot bash deploy/install-scheduler.sh`).

Ellenőrzés és kézi próba:

```bash
systemctl list-timers sol-tracker.timer      # mikor fut legközelebb
sudo systemctl start sol-tracker.service     # azonnali futtatás
tail -n 30 logs/signals.log                  # a futás naplója
systemctl status sol-tracker.timer           # aktív-e
```

A futtatott paraméterek (symbol, margin, leverage) a unit `ExecStart` sorában
vannak; módosítás után:

```bash
sudo systemctl edit --full sol-tracker.service   # vagy nano a fájlon
sudo systemctl daemon-reload
```

Cron-alternatíva (ha nem kell systemd):

```
2 * * * * cd /opt/binance-sol-tracker && flock -n /tmp/sol.lock .venv/bin/python pivot_signals.py --symbol SOLUSDC --margin-usdt 2 --leverage 20 --trade --notify-once >> logs/signals.log 2>&1
```

> A `--live` mód is használható a szerveren, de akkor systemd service kell
> `Restart=always`-szal; a timer + `--notify-once` egyszerűbb és hálózati
> megszakadás után is magától helyreáll.

## Pine-egyezés (jelzések és színek)

A Python-port a pivot breakout mellett a Pine volume spike, fordulat és SL/TP
utáni follow-through jelzéseit is számolja. Az alapértékek a Pine inputjai:
volume küszöb 2×, 10 gyertyás SMA, 5 gyertyás szigorú fordulatvizsgálat, 1 follow
gyertya, éjszakai ablak 00–07 (Europe/Budapest).

A **label színek pontosan a Pine logikáját követik** (a szín hordozza a jel
típusát, ahogy a TradingView-on):

| Szín | Jelentés |
|---|---|
| zöld | pivot BUY (`color.green`) |
| piros | pivot SELL (`color.red`) |
| rózsaszín | pivot BUY/SELL, ha kanócos (`pinkColor`, `wickRatio` felett) – csak `--show-pink-signal` mellett |
| kék | volume/fordulat BUY (`color.blue`) |
| narancs | volume/fordulat SELL (`color.orange`) |
| zöld / piros | követő (follow) BUY / SELL (`ftBuyColor` / `ftSellColor`) |
| fekete | volume/fordulat jelzés éjszaka (`nightColor`) |
| sárga / kék gyertyakeret | SL/TP érintés, illetve zárással áttörés (`barcolor`) |
| sárga **SPIKE** címke | volume spike (`showSpikeLabel`): mennyiség + ×arány az átlaghoz |

Minden címke alatt/felett egy **háromszög-nyíl** és egy vékony szár mutatja, hogy
melyik gyertyára vonatkozik (a TradingView `style_label_up` / `style_label_down`
mutatójának megfelelője).

A **SPIKE** címke minden volume spike-nál megjelenik (akkor is, ha abból nem lesz
BUY/SELL jelzés). A `--spike-alert` ugyanezt push értesítésként is elküldi – a
`last_signal.json`-ban külön `last_spike` időbélyeggel, így nem duplikál és nem
keveredik a jelzések állapotával.

Fontos részletek, amik a TradingView-hoz igazítják a képet:

- a pivot jelzés színe akkor is zöld/piros (vagy rózsaszín), ha éjszaka van –
  a fekete szín csak a volume/fordulat jelzésekre vonatkozik;
- ha egy gyertyán pivot **és** volume jelzés is van, egy címke jelenik meg, a
  pivot színével (a Pine `finalBuy = pivBuy or volBuy` logikája szerint);
- a kirajzolt jelzések előtt a szkript **bemelegítő gyertyákat** is letölt, így a
  jelzésállapot (függő volume jelzés, várakozó breakout, nyitott SL/TP, follow
  számláló) nem nullázódik a látható ablak elején – ez a leggyakoribb eltérés a
  TradingView-hoz képest;
- a `--trade` **a zöld/piros címkékre** kereskedik: a pivot breakoutra *és* a
  követő (follow) zárásra; a kék/narancs (volume, fordulat) és az éjszakai
  fekete jelzés csak értesítés. A follow jelzéshez a Pine is húz SL/TP vonalat:
  SL = a jelzett gyertya low (BUY) / high (SELL), TP = a **legközelebbi olyan
  pivot**, amit még semelyik korábbi gyertya nem ért el (a már elért szintek
  kikerülnek a követett listából); ha nincs ilyen, 2×ATR becslés. Ez a port is
  kirajzolja, és ezek a szintek a pivot szintekkel azonos módon **megbízásba is
  mennek**.
- a follow-ablak a Pine szerint **1 gyertya**: az SL/TP találat utáni *következő*
  gyertya zárása adhat follow jelzést (a `--follow-bars` ezt tudja növelni);
- a pivot szintcímkék (`--pivot-levels`) és a rózsaszín kanóc-jelzés
  (`--show-pink-signal`) a Pine-hoz hasonlóan alapból **ki** vannak kapcsolva,
  a jelzéslogikát nem érintik;

Például:

```powershell
python pivot_signals.py --volume-multiplier 2 --volume-lookback 10 --volume-ma wma
python pivot_signals.py --reversal-lookback 5 --reversal-mode majority --follow-bars 3
python pivot_signals.py --wick-ratio 2 --night-start 0 --night-end 7
python pivot_signals.py --plot --limit 300
python pivot_signals.py --plot --limit 300 --no-spike-label          # SPIKE címkék nélkül
python pivot_signals.py --live --notify --spike-alert                # spike push is
python pivot_signals.py --notify-once --intrabar                     # zárás előtti ráfutás (kék/narancs)
```

> **Fordulat mód:** a Pine „Szigorú” módja az utolsó `lookback` gyertya **mindegyikét**
> várja ellenkező színűnek (5-ből 5), a „Többség” csak a felét (`floor(n/2)+1`,
> azaz 5-ből 3). A `--reversal-mode majority` ezért **sokkal több** narancs/kék
> címkét ad (a TradingView-on is): ha ott több jelzést látsz, valószínűleg
> „Többség” módban fut az indikátor.

> A pivotoknál a Pine szigorú összehasonlítást használ: ha a ablakban van
> **pontegyenlő** high/low, az nem számít pivotnak. A Binance SOLUSDT 1h
> adatain ez a gyakorlatban nem fordul elő.

## Fontos tudnivalók

- **Időzítés / késés:** a GitHub cron **nem pontos** – terheléstől függően 1-10 percet
  (csúcsidőben többet) késhet, sőt egy-egy futás el is maradhat. A workflow ezért
  `:58`-kor indul és a job belül megvárja az órafordulót, így a jelzés az óra
  lezárulta után **pár másodperccel** kiértékelődik – de csak akkor, ha a GitHub
  időben elindítja. Ha a késés fontos (kézi belépés), érdemes always-on gépen
  `--live --notify` módban futtatni: ez a **gyertya zárására igazodik**, és amint
  a Binance publikálja az új gyertyát, azonnal (150 ms-os felbontással) kiértékel.
- **Kimaradt futás pótlása:** ha egy órai futás kimarad vagy elhal, a következő
  futás a `last_signal.json` alapján **pótolja** az összes kimaradt jelzést
  (biztonsági okból legfeljebb az utolsó 5-öt). Így jelzés nem veszik el.
- **Adatforrás / 451 hiba:** a GitHub Actions runnerek US-ban futnak, és a
  `api.binance.com` US IP-kről `451 Client Error`-t ad. Ezért a szkript
  alapertelmezésben a **`data-api.binance.vision`** hostot használja (ugyanaz a
  publikus piaci adat, nincs geo-blokk), és ha az nem elérhető, sorban megpróbálja
  a `api.binance.com` → `api.binance.us` hostokat. Kézzel: `--base-url <host>`.
- **Ingyenes:** publikus repóban korlátlan perc; privát repóban a havi 2000 ingyenes
  percből kb. 700-at használ (24 futás/nap × ~1 perc).
- Ha 60 napig nincs aktivitás a repóban, a GitHub **szünetelteti** az ütemezett
  workflow-t (e-mailt küld). Egy commit vagy kézi indítás újraindítja.
- A cron utcai idő szerint értendő.
- Az ntfy topic nevét **titokban** tartjuk (GitHub Secret), hogy illetéktelen ne
  iratkozhasson fel rá.
