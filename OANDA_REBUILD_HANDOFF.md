# Trading Bot — Handoff Kraken & Fondamenta per il Rebuild su OANDA

> Documento di passaggio. Riassume **tutto ciò che è stato costruito** per il bot
> di trading su Kraken, le **lezioni critiche apprese**, e i **requisiti del nuovo
> progetto** (OANDA: forex / indici / materie prime) con un sistema di backtesting
> molto più strutturato. Copialo nel nuovo repository come punto di partenza.

---

## 1. Scopo

Il primo progetto (bot autonomo crypto su Kraken) è servito a **costruire il motore e,
soprattutto, a imparare a testare in modo onesto**. Il nuovo progetto riparte da quella
base per:

- operare su **OANDA** (forex, indici, materie prime) invece che solo crypto;
- fare backtest su **molti più strumenti e timeframe**;
- definire **strategie precise** e validarle in modo **rigoroso** (out-of-sample) prima
  di rischiare denaro.

Il "cervello" (strategie, gestione del rischio, motore di backtest) è **riutilizzabile**.
Ciò che cambia è il **livello dati/esecuzione** (da Kraken/ccxt a OANDA) e l'ambizione del
**sistema di backtesting**.

---

## 2. Cosa è stato costruito (Kraken bot) — sintesi

Sistema completo H24 in Python:

- **Data engine** (ccxt/Kraken): candele OHLCV 1h + 15m.
- **Indicatori** nativi (pandas/numpy): EMA, RSI, MACD, ATR, OBV, Donchian, Ichimoku.
- **Strategie** selezionabili: `pullback`, `breakout`, `ichimoku`, `meanrev`.
- **Risk manager**: sizing 33%, stop ATR, gestione in 3 fasi (TP1 parziale + breakeven +
  trailing), limite di perdita giornaliera, **leva dinamica basata sul rischio**, short su
  margine, commissioni + costi di finanziamento.
- **Order manager**: ordini con retry, parametri margine (leva), modalità paper/live.
- **Engine**: loop decisionale, persistenza dello stato (crash-safe), interfaccia comandi.
- **Telegram bot** bidirezionale solo-proprietario (comandi + notifiche).
- **Backtest engine**: ottimizzatore a griglia, preset (`--aggressive`, `--active`,
  `--original`), **modellazione della liquidazione**, **walk-forward out-of-sample**.
- **Report**: win rate, profit factor, max drawdown, Sharpe, curva equity.
- **Paper trading** + test offline.
- **Deploy**: servizio `systemd` con riavvio automatico + installer one-command.

---

## 3. Architettura e moduli

```
config.py                 # Tutti i parametri (singola fonte di verità)
main.py                   # Entry point (--mode paper|live + override CLI)
modules/
  data_engine.py          # Dati di mercato (ccxt). Override exchange per i dati storici.
  indicators.py           # EMA, RSI, MACD, ATR, OBV, Donchian, Ichimoku (nativi)
  strategy.py             # Strategie + factory make_strategy(cfg)
  risk_manager.py         # Sizing, stop ATR, 3 fasi, leva dinamica, liquidazione, PnL+fee
  order_manager.py        # Esecuzione ordini (retry, margine), paper/live
  engine.py               # Loop, stato persistente, controller comandi Telegram
  telegram_bot.py         # Notifiche + comandi (owner-only)
  reporting.py            # Metriche di performance condivise
  logger.py               # Log testuale + log strutturato JSONL
backtest/
  data_fetcher.py         # Download/caching storico (sorgente configurabile)
  backtest_engine.py      # Backtest event-driven + optimize() + walk_forward()
  report.py               # Report + CLI (preset, --optimize, --walkforward)
tests/
  paper_trading.py        # Simulazione 48h
  test_core.py            # Test offline (indicatori/strategia/risk)
deploy/
  trading-bot.service     # systemd (auto-restart)
  setup.sh                # installer one-command
  DEPLOY.md               # guida deploy VPS
```

**Principio chiave da mantenere:** separare nettamente il **livello dati/esecuzione**
(broker-specifico) dal **cervello** (strategie + rischio + backtest), così cambiare broker
= riscrivere solo gli adapter.

---

## 4. Strategie implementate

Tutte producono un `Signal(side, price, atr, ...)` e usano la stessa gestione di
uscita (stop ATR + TP1 + trailing), salvo dove indicato.

- **pullback** (default): EMA20>EMA50 (trend) + RSI in banda + MACD (modalità "state" o
  "cross") + OBV. Numero di condizioni richieste configurabile (`min_conditions`).
- **breakout**: rottura del massimo/minimo degli ultimi N (Donchian) + filtro EMA.
- **ichimoku**: prezzo sopra/sotto la nuvola + Tenkan/Kijun.
- **meanrev**: RSI ipervenduto + momentum MACD + volume > media → rimbalzo.

---

## 5. Sistema di backtesting (com'è ora)

- **Event-driven**, barra per barra, **senza look-ahead**.
- Gestione intrabar (stop/liquidazione su estremo avverso, TP1/trailing su estremo
  favorevole).
- **Commissioni** (taker) + **costi di margine** (apertura + rollover ogni 4h) modellati.
- **Modellazione della liquidazione** (a leva alta) e rilevamento conto azzerato.
- `optimize()`: ricerca a griglia (ATR mult, RSI range, TP1, trailing step).
- `walk_forward()`: ottimizza su training, valida su dati **mai visti** → verdetto
  overfitting. **Strumento più importante** del sistema.
- Sorgente dati storica **configurabile** (`backtest_data_exchange`) perché Kraken è
  limitato (vedi lezioni).

---

## 6. ⚠️ LEZIONI CRITICHE APPRESE (la parte più importante)

1. **Kraken (API pubblica) fornisce solo ~720 candele.** I primi backtest "12 mesi"
   giravano in realtà su ~30 giorni (1h) / ~7 giorni (15m). → **Tutti i risultati iniziali
   erano inaffidabili.** Soluzione adottata: scaricare lo storico da una fonte con storia
   profonda (Binance) mantenendo Kraken solo per il live. **Per OANDA: verificare la
   profondità storica reale per ogni strumento/timeframe.**

2. **Nessuna delle 7 strategie testate aveva un edge reale** su 12 mesi di dati veri.
   La migliore (pullback originale, 1x) andava in **pareggio** (profit factor ~0,91).
   Tutto il resto perdeva.

3. **Più trade ≠ più profitto.** Allentare le condizioni (più operazioni) ha **peggiorato**
   i risultati (qualità dei segnali crollata).

4. **Più leva ≠ più guadagno.** All-in a 10x → conto **azzerato** su 12 mesi (liquidazione).
   La leva amplifica soprattutto le perdite.

5. **L'overfitting è il nemico n.1.** Cercare "la strategia che funziona sul passato"
   produce illusioni che perdono dal vivo. → **Il walk-forward (out-of-sample) è
   obbligatorio**: se non regge sui dati mai visti, si scarta.

6. **Aumentare il capitale non sistema una strategia perdente** — moltiplica le perdite.

7. **Validare PRIMA di rischiare denaro.** La sequenza è: backtest onesto → walk-forward →
   paper trading → solo dopo, eventuale live con denaro che ci si può permettere di perdere.

> Sintesi: il valore di questo lavoro non è "un bot che fa soldi" (non lo fa), ma un
> **metodo onesto** per distinguere un edge reale da un'illusione, ed evitare di perdere
> denaro.

---

## 7. Risultati backtest Kraken (sintesi onesta, 12 mesi dati reali)

| Strategia / preset            | Esito |
|-------------------------------|-------|
| Pullback originale (4 cond., 1x) | ~pareggio (−3€, PF 0,91) |
| Mean-reversion                | perdita |
| Breakout                      | forte perdita |
| Ichimoku                      | forte perdita |
| Pullback "attiva" (leva 5x)   | forte perdita |
| Aggressiva (all-in 10x)       | conto azzerato |
| **Walk-forward (best params)**| in-sample +10€ → **out-of-sample −45€ → OVERFITTING** |

---

## 8. Cosa riusare vs cosa rifare per OANDA

**Riusabile quasi as-is:**
- `indicators.py`, `strategy.py` (+ factory), `risk_manager.py`, `reporting.py`,
  `backtest_engine.py` (optimize + walk_forward), `logger.py`, struttura `engine.py`.

**Da riscrivere (adapter broker):**
- **Data layer** → API OANDA (storico profondo, multi-timeframe, multi-strumento).
- **Execution layer** → ordini OANDA (forex/CFD: spread, swap, leva regolamentata UE,
  orari di mercato, gestione weekend/festivi).
- **Costi** → modellare **spread + swap notturno** (non commissione % come crypto).

**Da considerare nuovo:**
- Mercati con **orari** (non 24/7): il sistema deve sapere quando il mercato è
  aperto/chiuso, gap del weekend, sessioni (Londra/NY/Tokyo).
- Strumenti con caratteristiche diverse (pip value, contract size, margini per asset).

---

## 9. Requisiti del NUOVO progetto (OANDA, backtest strutturato)

**Obiettivo:** sistema di backtesting molto più strutturato per validare strategie precise
su molti strumenti e timeframe, in modo rigoroso e onesto.

**Da implementare:**
1. **Adapter dati OANDA**: download storico profondo, cache per (strumento, timeframe),
   verifica copertura reale. Conto **demo gratuito** per dati + paper.
2. **Multi-timeframe**: pipeline che gestisce M1, M5, M15, M30, H1, H4, D1… con
   allineamento corretto e niente look-ahead.
3. **Multi-strumento**: forex majors/minors, indici (US500, NAS100, GER40…), materie
   prime (XAU/USD oro, petrolio…). Parametri per-strumento (spread, swap, pip).
4. **Costi realistici**: spread variabile + swap + (eventuale commissione OANDA).
5. **Motore strategie modulare**: ogni strategia = entrata + uscita; libreria di
   indicatori estesa; parametri configurabili.
6. **Validazione rigorosa (NON negoziabile)**:
   - train/test split + **walk-forward** (già presente, da potenziare);
   - eventuale **walk-forward "rolling"** (più finestre) per robustezza;
   - report con metriche per strumento/timeframe + aggregato;
   - criterio esplicito **"scala o scarta"** basato sull'out-of-sample.
7. **Gestione orari di mercato** (sessioni, weekend, festivi).
8. **Paper trading su demo OANDA** prima del live.
9. **Deploy**: riuso del **VPS Hetzner** esistente (systemd, già configurato per il bot
   Kraken — adattare ExecStart/credenziali).

**Disciplina da mantenere:** validare prima, costruire poi. Niente "live" senza
backtest profondo + walk-forward positivo + paper trading.

---

## 10. Deployment (Hetzner VPS — già attivo)

- VPS Ubuntu 24, accesso SSH.
- Bot eseguito come **servizio systemd** (`Restart=always`, `enabled` = avvio al boot).
- Credenziali in `/opt/<progetto>/.env` (mai committate; `chmod 600`).
- Per il nuovo progetto: nuovo `.env` con le chiavi **OANDA** (account id + token; partire
  dal **demo**), nuovo service file, stessa logica di installazione.

---

## 11. Stack tecnico / dipendenze

- Python 3.11
- `pandas`, `numpy` (indicatori e backtest nativi)
- `ccxt` (Kraken — sostituibile/affiancabile con client OANDA: es. `oandapyV20`)
- `python-telegram-bot` (notifiche/comandi)
- `python-dotenv`, `matplotlib`
- Indicatori implementati **a mano** (non si dipende da `pandas-ta`, fragile con numpy 2.x)

---

## 12. Prossimi passi consigliati

1. Creare il **nuovo repository** e copiare qui dentro i moduli riusabili (sezione 8).
2. Aprire un **account demo OANDA** → ottenere `account_id` + `API token`.
3. Costruire **prima l'adapter dati OANDA** + verificare profondità storica per
   strumento/timeframe (lezione n.1).
4. Estendere il **motore di backtest** a multi-strumento/multi-timeframe + costi
   (spread/swap) + orari di mercato.
5. Definire **1-2 strategie precise** e validarle con **walk-forward** su più strumenti.
6. Solo se reggono out-of-sample → **paper trading su demo** → eventuale live.

> Ricorda la lezione madre: il backtest serve a **scoprire la verità**, non a confermare
> una speranza. Se l'edge non c'è, è un risultato prezioso (ti fa risparmiare denaro).
