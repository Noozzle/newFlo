# AI Gate — Повний гайд

## Зміст

1. [Огляд](#огляд)
2. [Структура проекту](#структура-проекту)
3. [Скрипти та команди](#скрипти-та-команди)
4. [Повний процес підготовки та тренування](#повний-процес-підготовки-та-тренування)
5. [Використання навченої моделі](#використання-навченої-моделі)
6. [Допоміжні скрипти](#допоміжні-скрипти)

---

## Огляд

AI Gate — фільтр сигналів стратегії `OrderflowStrategy`. Для кожного сигналу на вхід модель приймає рішення:

| Дія    | Ефект                  |
|--------|------------------------|
| `SKIP` | Позиція не відкривається |
| `HALF` | `risk *= 0.5`          |
| `FULL` | `risk *= 1.0` (без змін) |

Мета: відсікати слабкі сигнали (SKIP/HALF), залишати сильні (FULL).

---

## Структура проекту

```
scripts/ai_gate/          ← Основний пайплайн (v2)
  ├── normalize.py        ← Крок 1: CSV → Parquet
  ├── build_mytrades_dataset.py  ← Крок 2a: датасет з реальних угод
  ├── generate_signals.py ← Крок 2b: датасет з replay стратегії
  ├── train.py            ← Крок 3: тренування моделі
  └── infer.py            ← Крок 4: оцінка / інференс

model/                    ← Артефакти навченої моделі
  ├── ai_gate_model.pkl   ← HistGradientBoostingClassifier
  ├── ai_gate_regressor.pkl ← HistGradientBoostingRegressor
  └── config.json         ← Фічі, пороги, метадані

data/
  ├── normalized/         ← Нормалізовані Parquet-файли
  │   ├── {SYMBOL}/
  │   │   ├── candles_1m.parquet
  │   │   ├── candles_15m.parquet
  │   │   ├── l1.parquet
  │   │   └── exchange_trades.parquet
  │   └── my_trades.parquet
  └── datasets/           ← Розмічені датасети для тренування
      ├── mytrades_labeled.parquet
      ├── signals_labeled.parquet
      └── signals_sl_tp_labeled.parquet

app/trading/ai_gate.py   ← Рантайм-гейт (live/backtest)
```

---

## Скрипти та команди

### 1. `scripts/ai_gate/normalize.py` — Нормалізація даних

Конвертує сирі CSV із `live_data/` та `live_trades/` у єдиний Parquet-формат з int64-ms таймстемпами.

```bash
# Один символ
python -m scripts.ai_gate.normalize --symbol DOGEUSDT --out data/normalized

# Всі символи
python -m scripts.ai_gate.normalize --all --out data/normalized
```

| Аргумент | За замовчуванням | Опис |
|----------|------------------|------|
| `--symbol` | — | Один символ для нормалізації |
| `--all` | — | Нормалізувати всі символи з `live_data/` |
| `--out` | `data/normalized` | Вихідна директорія |
| `--live-data` | `live_data` | Директорія з сирими CSV |
| `--live-trades` | `live_trades` | Директорія з реальними угодами |

**Вхід → Вихід:**
- `live_data/<SYMBOL>/1m.csv` → `data/normalized/<SYMBOL>/candles_1m.parquet`
- `live_data/<SYMBOL>/15m.csv` → `data/normalized/<SYMBOL>/candles_15m.parquet`
- `live_data/<SYMBOL>/orderbook.csv` → `data/normalized/<SYMBOL>/l1.parquet`
- `live_data/<SYMBOL>/trades.csv` → `data/normalized/<SYMBOL>/exchange_trades.parquet`
- `live_trades/trades_*.csv` → `data/normalized/my_trades.parquet`

---

### 2a. `scripts/ai_gate/build_mytrades_dataset.py` — Датасет з реальних угод

Будує розмічений датасет із фактичних угод (my_trades), джоїнить з ринковими даними на момент входу.

```bash
python -m scripts.ai_gate.build_mytrades_dataset --symbols DOGEUSDT,ETHUSDT --in data/normalized --out data/datasets
```

| Аргумент | За замовчуванням | Опис |
|----------|------------------|------|
| `--symbols` | — | Через кому або `all` |
| `--in` | `data/normalized` | Нормалізовані дані |
| `--out` | `data/datasets` | Вихідна директорія |

**Вихід:** `data/datasets/mytrades_labeled.parquet`

**Фічі (26):** rel_spread, ob_imbalance, tape-фічі (buy/sell vol, delta, trade_count, avg_trade_size, delta_ratio) для вікон 30s/60s/120s, range_pct_1m, close_pos_1m, atr_14_1m, atr_14_15m, close_minus_ma20_15m, trend_slope_15m.

**Лейбли:** `net_return = net_pnl / notional`, `win = 1 якщо net_pnl > 0`.

> Зазвичай дає малу кількість семплів (~100). Для тренування краще використовувати `generate_signals.py`.

---

### 2b. `scripts/ai_gate/generate_signals.py` — Датасет з replay стратегії (основний)

Програє стратегію `OrderflowStrategy` по історичним даним, збирає всі сигнали на вхід (позиція завжди "вільна"), та розмічає результати.

```bash
# SL/TP симуляція (рекомендовано)
python -m scripts.ai_gate.generate_signals --label-mode sl_tp_sim

# 30-хвилинний горизонт
python -m scripts.ai_gate.generate_signals --label-mode horizon
```

| Аргумент | За замовчуванням | Опис |
|----------|------------------|------|
| `--config` | `config.yaml` | Конфіг стратегії |
| `--norm-dir` | `data/normalized` | Нормалізовані дані |
| `--out` | авто | Вихідний файл |
| `--start` / `--end` | з конфігу | Діапазон дат |
| `--label-mode` | `sl_tp_sim` | Спосіб розмітки |
| `--max-horizon` | `180` | Макс. горизонт (хв) для sl_tp_sim |

**Режими розмітки:**

| Режим | Вихід | Як працює |
|-------|-------|-----------|
| `sl_tp_sim` | `signals_sl_tp_labeled.parquet` | Симулює SL/TP по 1m свічкам вперед. Якщо обидва зачеплені одночасно — SL виграє. Ураховує slippage = rel_spread/2. |
| `horizon` | `signals_labeled.parquet` | Фіксований вихід через 30 хвилин. |

**Вихід:** ~24 000 семплів із 27 фічами + лейблами `net_return`, `win`, `sl_dist_return`.

> Це **основний генератор датасету** для тренування.

---

### 3. `scripts/ai_gate/train.py` — Тренування моделі

Тренує класифікатор (HistGradientBoostingClassifier) з walk-forward валідацією.

```bash
# Walk-forward (рекомендовано)
python -m scripts.ai_gate.train --data data/datasets/signals_sl_tp_labeled.parquet --out model

# Одиночний спліт 70/30
python -m scripts.ai_gate.train --data data/datasets/signals_sl_tp_labeled.parquet --mode single --out model
```

| Аргумент | За замовчуванням | Опис |
|----------|------------------|------|
| `--data` | `data/datasets/signals_sl_tp_labeled.parquet` | Датасет |
| `--out` | `model` | Директорія для збереження моделі |
| `--mode` | `wf` | `wf` (walk-forward) або `single` (70/30) |
| `--n-folds` | `5` | Кількість фолдів walk-forward |
| `--k-values` | `20,30,40,50` | Значення K для TOPK grid search |
| `--train-ratio` | `0.7` | Пропорція train для single mode |

**Walk-forward процес:**
1. Сортує датасет по `ts_entry`
2. Ділить на `n_folds + 1` блоків
3. Для кожного фолду i: train = блоки 0..i, test = блок i+1
4. Тренує класифікатор із зваженими семплами (class-balanced)
5. Шукає оптимальний K (TOPK): top K% сигналів по p_win → FULL, решта → HALF
6. Обирає найкращий K по медіані delta (gated vs baseline)
7. Фінальна модель тренується на всіх даних

**Вихід:**
- `model/ai_gate_model.pkl` — класифікатор
- `model/ai_gate_regressor.pkl` — регресор (тільки в single mode)
- `model/config.json` — фічі, пороги, метадані walk-forward

---

### 4. `scripts/ai_gate/infer.py` — Оцінка та інференс

```bash
# Оцінка моделі на датасеті
python -m scripts.ai_gate.infer evaluate --model model --data data/datasets/signals_sl_tp_labeled.parquet

# Одиночний прогноз
python -m scripts.ai_gate.infer predict --model model --features '{"rel_spread":0.0001, ...}'
```

| Підкоманда | Аргументи | Опис |
|------------|-----------|------|
| `evaluate` | `--model`, `--data`, `--t2` (опціонально) | Порівнює baseline (all FULL) vs gated, свіпить пороги |
| `predict` | `--model`, `--features` (JSON) | Прогноз для одного сигналу |

**Evaluate виводить:** sum(return), mean(return), max_drawdown для baseline та gated. Свіп t2 (0.30–0.70) та TOPK (K=10–50%).

---

### 5. `scripts/train_gate.py` — Легасі тренер (v1)

> **Застарілий.** Використовує LightGBM та 10 фіч з `FeatureSnapshot`. Працює з `gate_signals.csv` + `trades.csv` з бектесту.

```bash
python scripts/train_gate.py --signals gate_signals.csv --trades reports/<run>/trades.csv
```

| Аргумент | За замовчуванням | Опис |
|----------|------------------|------|
| `--signals` | — | gate_signals.csv з бектесту |
| `--trades` | — | trades.csv з того ж бектесту |
| `--output` | `models/gate.joblib` | Шлях збереження моделі |
| `--full-threshold` | `0.5` | Поріг для FULL |
| `--half-threshold` | `0.3` | Поріг для HALF |

---

## Повний процес підготовки та тренування

### Крок 0. Збір сирих даних

Запусти бота в live-режимі. Він записує:
- `live_data/<SYMBOL>/1m.csv` — 1-хвилинні свічки
- `live_data/<SYMBOL>/15m.csv` — 15-хвилинні свічки
- `live_data/<SYMBOL>/orderbook.csv` — L1 orderbook (bid/ask)
- `live_data/<SYMBOL>/trades.csv` — exchange trades (стрічка)
- `live_trades/trades_YYYYMMDD.csv` — реальні угоди бота

> Чим більше днів даних — тим краще. Мінімум 7–10 днів.

### Крок 1. Нормалізація

```bash
python -m scripts.ai_gate.normalize --all --out data/normalized
```

Конвертує всі CSV у Parquet. Таймстемпи стають int64 (мс). Orderbook отримує колонку `spread`.

### Крок 2. Генерація датасету

**Рекомендований шлях** — replay стратегії з SL/TP симуляцією:

```bash
python -m scripts.ai_gate.generate_signals --label-mode sl_tp_sim
```

Що відбувається:
1. **Фаза 1** — підіймає стратегію та EventBus, програє весь історичний період. Збирає кожен сигнал на вхід (entry_price, sl_price, tp_price).
2. **Фаза 2** — для кожного сигналу будує 27 фіч з ринкових даних **на момент входу** (без data leakage: тільки дані з `ts <= ts_entry`).
3. **Фаза 3** — розмічає результат кожного сигналу: ітерує 1m свічки вперед, перевіряє чи зачепився SL або TP. Рахує `net_return` з урахуванням комісій та slippage.

Результат: `data/datasets/signals_sl_tp_labeled.parquet` (~24 000 семплів).

**Альтернативний шлях** — якщо є достатньо реальних угод:

```bash
python -m scripts.ai_gate.build_mytrades_dataset --symbols all
```

### Крок 3. Тренування

```bash
python -m scripts.ai_gate.train --data data/datasets/signals_sl_tp_labeled.parquet --out model --mode wf
```

Walk-forward процес (5 фолдів):
1. Дані сортуються по часу.
2. Для кожного фолду: тренування на минулому, тест на наступному блоці.
3. Grid search по K = {20, 30, 40, 50} (% сигналів, які отримають FULL).
4. Обирається K з найкращою медіаною delta (різниця gated vs baseline sum return).
5. Фінальна модель навчається на всіх даних.

Результат:
- `model/ai_gate_model.pkl` — sklearn HistGradientBoostingClassifier
- `model/config.json` — конфігурація (фічі, topk_pct, cutoff)

### Крок 4. Оцінка

```bash
python -m scripts.ai_gate.infer evaluate --model model --data data/datasets/signals_sl_tp_labeled.parquet
```

Перевіряє: чи gated стратегія перевершує baseline? Виводить:
- Baseline vs Gated: sum(return), mean(return), max_drawdown
- Свіп порогів t2 та K

### Крок 5. Перетренування (коли додаються нові дані)

Коли накопичуються нові дні даних:

```bash
# 1. Нормалізуй нові дані
python -m scripts.ai_gate.normalize --all --out data/normalized

# 2. Перегенеруй датасет
python -m scripts.ai_gate.generate_signals --label-mode sl_tp_sim

# 3. Перетренуй модель
python -m scripts.ai_gate.train --data data/datasets/signals_sl_tp_labeled.parquet --out model

# 4. Оціни
python -m scripts.ai_gate.infer evaluate --model model --data data/datasets/signals_sl_tp_labeled.parquet
```

---

## Використання навченої моделі

### Рантайм-інтеграція

Модель підключається через `config.yaml`:

```yaml
ai_gate:
  enabled: true
  model_path: models/gate.joblib   # шлях до моделі
  full_threshold: 0.5              # p_win >= 0.5 → FULL
  half_threshold: 0.3              # p_win >= 0.3 → HALF, інакше SKIP
  fallback_action: full            # дія коли модель відсутня
  log_signals: true                # логувати рішення
  log_path: gate_signals.csv       # файл логів
```

### Ланцюг виконання (engine → gate → order_manager)

```
Strategy  ──SignalEvent──▶  Engine._on_signal()
                              │
                              ▼
                         AIGate.decide(signal)
                              │
                    ┌─────────┼─────────┐
                    │         │         │
                  SKIP      HALF      FULL
                    │         │         │
                 return   risk*=0.5  risk*=1.0
                    │         │         │
                    ×         └────┬────┘
                                  ▼
                         OrderManager.execute_entry()
```

1. Стратегія емітить `SignalEvent` з метаданими (фічі).
2. Engine передає сигнал у `AIGate.decide()`.
3. AIGate витягує фічі, запускає `model.predict_proba()`, отримує `p_win`.
4. По порогам визначає дію: SKIP / HALF / FULL.
5. SKIP → позиція не відкривається.
6. HALF → `risk_scale = 0.5` (розмір позиції вдвічі менший).
7. FULL → стандартний розмір.

### Логування

При `log_signals: true` кожне рішення записується у `gate_signals.csv`:
```
volume_imbalance,atr_pct,spread_pct,...,action,p_win,expected_r,reason
```

Ці логи можна використати для аналізу та перетренування (через `scripts/train_gate.py`).

---

## Допоміжні скрипти

### `scripts/walkforward_eval.py` — Оптимізація параметрів стратегії

Оптимізує гіперпараметри стратегії (не AI gate) методом walk-forward.

```bash
python scripts/walkforward_eval.py --config config.yaml --from 2026-02-03 --to 2026-02-10 --splits 3
```

Генерує комбінації параметрів (imbalance_threshold, delta_threshold, atr_multiplier, rr_ratio), запускає бектести, оцінює OOS-результати.

### `scripts/convert_to_parquet.py` — CSV → Parquet

Конвертує CSV у `live_data/` в Parquet (на місці, поруч з CSV). Не плутати з `normalize.py`, який пише у `data/normalized/`.

```bash
python scripts/convert_to_parquet.py                # всі символи
python scripts/convert_to_parquet.py --symbol SOLUSDT
python scripts/convert_to_parquet.py --dedup         # дедуплікація orderbook
```

### `scripts/regression_test.py` — Тест CSV vs Parquet

Перевіряє, що бектест на CSV та Parquet дає ідентичні результати.

```bash
python scripts/regression_test.py --date 2026-02-03 --tolerance 1e-8
```

### `scripts/aggregate_orderbook.py` / `scripts/aggregate_all_data.py` — Стиснення

Зменшують розмір файлів: orderbook → 1 запис/сек, klines → 1 запис/хв.

### `scripts/check_atr.py` — Діагностика ATR

Аналізує ATR для SOLUSDT, рахує порогові значення для cost-vs-risk ліміту risk_manager.

---

## Шпаргалка: Повний пайплайн за 4 команди

```bash
# 1. Нормалізація
python -m scripts.ai_gate.normalize --all

# 2. Генерація датасету
python -m scripts.ai_gate.generate_signals --label-mode sl_tp_sim

# 3. Тренування
python -m scripts.ai_gate.train --mode wf --out model

# 4. Оцінка
python -m scripts.ai_gate.infer evaluate --model model --data data/datasets/signals_sl_tp_labeled.parquet
```
