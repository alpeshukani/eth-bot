# LSR ETH Bot — Free 24/7 Cloud Version

ETHUSD **1H Liquidity Sweep Reversal** bot. GitHub Actions par chalta hai, matlab **PC/laptop band ho ya internet kat jaye — bot phir bhi chalega**. Default **paper mode** (koi real order nahi).

| File | Kaam |
|---|---|
| `algo_bot.py` | Main bot (paper mode) — strategy ka poora engine |
| `.github/workflows/bot.yml` | Har ghante chalne ka schedule + state commit |
| `requirements.txt` | Python libraries (pandas, numpy, requests) |
| `bot_state.json` | Equity, position, pending order ka record |
| `bot_trades.csv` | Har entry/exit ka permanent record |
| `docs/index.html` | Live dashboard (GitHub Pages) |
| `.gitignore` | Secret files ko upload hone se rokta hai |

> ⚠️ **`bot_config.txt` kabhi upload mat karna** — usme Telegram token hota hai. Cloud mein token GitHub *Secrets* se aata hai.

---

## Strategy kya hai (1 minute mein)

1. **Levels** banao: previous day high/low, current day high/low, Asia session high/low, aur equal highs/lows.
2. **Sweep** dhoondo: candle ka wick level ke paar jaye (kam se kam 0.1 × ATR), par **close wapas level ke andar** aaye.
3. Us level par **LIMIT order** lagao (market order nahi) — 3 ghante tak valid.
4. **SL** = sweep ke extreme se 0.3 × ATR aage. **TP** = 1R.
5. **12 ghante** baad trade khud band (time stop).
6. Filter: sirf **07:00–20:00 UTC** (London/NY), EMA200 ke sahi side, din mein **max 2 trades**.

**Backtest (1 saal ETH 1h, real Delta fees):** 426 trades · WR 59.4% · PF 1.33 · net +175.6% · MaxDD −22.5%

Ye numbers guarantee nahi hain — lekin strategy ka edge sirf **ETH 1h + limit entry + session filter** par mila. Isi liye:
- BTC par yahi logic **−23.5%** deta hai
- 15m par **−22.9%**
- taker (market) entry par **−72.4%**

Isliye bot ka `--live` mode **jaan-boojh kar nahi banaya**. Is strategy ka edge limit-order management par tika hai; paper mein 4+ hafte proof ke baad hi live sochna.

---

## Bot ki 3 khaas baatein (jo pichhli galtiyan theek karti hain)

1. **Bar-by-bar replay** — har run par pichhle run ke baad ke **saare** closed 1H bars dobara process hote hain. GitHub run late ho, miss ho, ya 6 ghante baad aaye — koi bar, sweep, SL ya TP chhootega nahi.
2. **Pending/position state file mein** — bot crash ho ya run fail ho, agli baar wahin se continue.
3. **Honest fills** — entry limit price par (maker fee), exit SL/TP price par slippage ke saath (2 ticks), time-stop current close par taker fee. Koi "free fill" nahi.

Parity test: naya live code purane backtest engine se **24/24 trades match**, equity difference 0.000004 — matlab jo backtest mein chala wahi live chalega.

---

## Setup (ek baar, ~15 minute)

### 1. Naya repository banao
GitHub par **`+` → New repository**
- Name **exactly**: `eth-bot` (dashboard isi naam se data padhta hai)
- **Public** chuno (free unlimited Actions minutes)
- **Create repository**

### 2. Poora folder upload karo
Repo page par **Add file → Upload files** → apne PC se `E:\Algo Trading\eth-bot-cloud` ka **andar ka saara content** drag karo:
`algo_bot.py`, `requirements.txt`, `bot_state.json`, `bot_trades.csv`, `.gitignore`, `docs/index.html`, `.github/workflows/bot.yml`

> `.github` folder upload na ho to: **Add file → Create new file** → filename `.github/workflows/bot.yml` → content paste → Commit. (`.gitignore` aur `docs/index.html` bhi isi tarah.)

### 3. Telegram secrets daalo
Repo → **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather wala token |
| `TELEGRAM_CHAT_ID` | aapki chat id |

Naam **bilkul same** hone chahiye (capital letters).

### 4. Actions enable + test
Repo → **Actions** → "I understand my workflows..." aaye to click → left side **LSR ETH Bot** → **Run workflow** → **Run workflow**.

Green ✅ + Telegram par 2 message (test + heartbeat) = sab theek.

### 5. Dashboard on karo
Repo → **Settings → Pages** → Source `Deploy from a branch` → Branch `main` · Folder `/docs` → **Save**.

1–2 minute baad: **https://alpeshukani.github.io/eth-bot/**

Phone par: Chrome mein kholo → **3 dots → Add to Home screen**.

---

## ⚠️ Sabse zaroori baat — GitHub ka cron reliable nahi hai

Ye sugar-coating nahi, measurement hai. Gold bot ke pichhle 46 ghante mein GitHub ne **4 baar** chalaya (hourly likha tha):

```
23:50 UTC → 06:14 UTC (6.4 ghante gap) → 14:40 UTC
```

Public repos par GitHub free scheduler ko throttle karta hai. ETH 1h strategy ke liye 6 ghante ka gap matlab: kai sweep miss, aur pending limit/TP check late.

**Bot ka replay design isse bacha leta hai** (koi bar/level nahi chhootta), lekin entry/exit ka timing thoda different ho sakta hai. Poori reliability ke liye 2 option:

### Option A — cron-job.org (free, 10 minute) ✅ recommended

Ye har ghante bahar se GitHub workflow ko trigger karega, GitHub ke throttle ko bypass karke.

1. GitHub par **fine-grained token** banao: Settings → Developer settings → **Personal access tokens → Fine-grained tokens → Generate new token**
   - Repository access: **Only select repositories** → `eth-bot`
   - Permissions: **Actions → Read and write**
   - Expiry: 1 year (calendar reminder laga lo)
2. [cron-job.org](https://cron-job.org) par free account banao → **Create cronjob**
   - Title: `eth-bot`
   - URL: `https://api.github.com/repos/alpeshukani/eth-bot/actions/workflows/bot.yml/dispatches`
   - Schedule: **Every 1 hour** (minute 5)
   - **Advanced → Request method: POST**
   - Headers:
     - `Authorization: Bearer <aapka token>`
     - `Accept: application/vnd.github+json`
   - Body: `{"ref":"main"}`
3. Save → pehli baar **"Test run"** dabao. Response **204** aana chahiye (204 = accepted, body khaali hoti hai).

Iske baad har ghante workflow chalega — GitHub ka apna schedule bhi rahega, par ab delay ka issue nahi.

> Token sirf `eth-bot` repo ke Actions tak limited hai. Phir bhi: token kisi ko share mat karna, aur expire hone se pehle naya banao.

### Option B — Oracle Cloud Always Free VM (advanced, 30–45 min)

Bot **har minute** chalega, GitHub par depend nahi. Setup technical hai — bolo to main step-by-step commands de dunga. Short version:

1. Oracle Cloud free account → **Always Free** VM (AMD micro ya ARM, Ubuntu 22.04).
2. VM par Python 3 + git install, repo clone.
3. `pip install -r requirements.txt`
4. `python algo_bot.py --loop 60` ko **systemd service** bana do (VM reboot par khud chalu).
5. State ko GitHub par sync karne ke liye har 5 minute ek `sync.sh` (git pull, commit, push) — dashboard isi se update hota hai.
6. **GitHub Actions ka schedule disable kar do** (warna dono jagah se trade lagega).

---

## Roz kya hoga

- Har ghante (ya VM par har minute) check
- **Din mein ek baar** heartbeat message (price, equity, position, pending order)
- Sweep mila → Telegram par **LIMIT ORDER** message
- Fill hua → **ENTRY** message (SL/TP ke saath)
- SL/TP/time stop → **EXIT** message (P&L, R-multiple)
- `bot_state.json` + `bot_trades.csv` repo mein auto-update

**Band karna:** Actions → LSR ETH Bot → `...` → **Disable workflow**.
**Permanently delete:** Settings → Danger Zone → Delete repository.

---

## Local test (PC par, GitHub ke bina)

```powershell
cd "E:\Algo Trading\eth-bot-cloud"
pip install -r requirements.txt
python algo_bot.py --once          # ek check
python algo_bot.py --selftest      # live code ko history par replay + stats
python algo_bot.py --loop 60       # har 60 sec (VM mode)
```

> `--selftest` asli `bot_trades.csv` mein rows add karta hai. Test ke baad file ka backup rakhna.

---

## 📱 Dashboard mein kya dikhta hai

| Hissa | Detail |
|---|---|
| **Chart** | 15m / 1H / 4H candles, ENTRY + SL + **TP** lines, pending limit order line, last price |
| **Position** | Side, size, entry, mark, SL, TP, unrealised P&L, ROE, R, 1R progress bar |
| **Account** | Equity (open P&L ke saath), return, closed trades, win rate |
| **Bot status** | Last run, next run (IST), heartbeat, last closed 1H bar |
| **Tabs** | Positions · Trade History (level ke saath) · Bot Activity · Performance |

Read-only hai — dashboard se koi order place/cancel nahi hota. Har 20 second auto-refresh.

---

## Satik baatein (no sugar-coating)

1. **Paper mode** hai — koi real order nahi jata. ✅
2. **Live mode is bot mein nahi hai** (jaan-boojh kar). Paper mein 4+ hafte, phir sochenge.
3. GitHub ka schedule exact nahi chalta — Option A/B lagao warna results backtest se different honge.
4. **SL/TP check har ghante** hota hai (har tick nahi). 1H strategy ke liye theek hai, par wick jo ghante ke andar aayi aur wapas chali gayi — wo fill backtest jaisi exact nahi milegi.
5. Repo 60 din inactive rahe to GitHub schedule band karta hai — par ye bot khud state commit karta hai, isliye problem nahi.
6. Backtest 426 trades par PF 1.33 — **isko guarantee na samjho**. 20 trade ka losing streak bhi is system mein normal hai.
7. Fees/slippage already included hain (maker 0.02%, taker 0.05%, 2 ticks exit slippage).
