# SOL Pivot Signals – ingyenes futtatás GitHub Actions-szel

Binance SOL 1h pivot-breakout jelzések + push értesítés a telefonra.
Szerver nélkül, ingyen: a GitHub Actions óránként lefuttatja a szkriptet.

## Fájlok

| Fájl | Szerep |
|---|---|
| `sol_tracker.py` | Egyszerű árfolyam-figyelő másodpercenként |
| `pivot_signals.py` | A Pine Script portja + értesítés |
| `notify.py` | Push küldés (ntfy / Telegram / Discord / Pushover) |
| `.env.example` | Értesítési beállítások sablonja |
| `.github/workflows/signals.yml` | Az óránkénti futtatás (GitHub Actions) |

## 1. Értesítés beállítása

Válassz egy csatornát. A legegyszerűbb az **ntfy** (ingyenes, nem kell regisztráció):

1. Telepítsd a **ntfy** appot a telefonra
2. Válassz egy egyedi topic nevet, pl. `sol-pivot-9f3a2c7e`
3. Az appban: **Subscribe** → add meg ugyanezt a topic nevet
4. Helyben teszteld:

```powershell
python pivot_signals.py --test-notify
```

> Éles módban (`--live --notify`) a szkript **induláskor azonnal** küld egy
> "figyelés elindult" értesítést, így rögtön látod, hogy él a kapcsolat.

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
| `NTFY_TOPIC` | a topic neved, pl. `sol-pivot-9f3a2c7e` |
| `TELEGRAM_BOT_TOKEN` | *(ha Telegramot használsz)* |
| `TELEGRAM_CHAT_ID` | *(ha Telegramot használsz)* |
| `DISCORD_WEBHOOK_URL` | *(ha Discordot használsz)* |

## 4. Ellenőrzés

**Actions** fül → `Pivot signals -> push` → **Run workflow** (kézi indítás).

Ezután magától fut minden órában a `3 * * * *` cron szerint, és csak akkor küld
értesítést, ha az éppen lezárt 1h gyertyán jelzés keletkezett.

> **Teszt értesítés a GitHubról:** a *Run workflow* panelen pipáld be a
> **„Csak teszt értesítés küldése"** opciót → így azonnal kapsz egy teszt push-t a
> telefonodra (ilyenkor nem fut jelzés-ellenőrzés és nem módosul az állapot).
> A sima (kijelöletlen) futtatás csak akkor küld, ha tényleg van új jelzés.

## Fontos tudnivalók

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
