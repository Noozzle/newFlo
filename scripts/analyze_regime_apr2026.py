"""Regime-деградація аналіз FloTrader, квітень 2026.

Джерела:
- Live trades: D:/Trading/newFlo/liveLoad/trades_2026040{9..16}.csv  (8 днів, 32 трейди)
- Свічки Bybit: D:/Trading/newFlo/live_data/{SYM}/{1m,5m}_apr2026.parquet (UTC ms)

Output: D:/Trading/newFlo/reports/regime_apr2026/
- regime_daily.csv       — щоденні regime метрики по символах на 5m/1m
- trades_enriched.csv    — трейди + regime-контекст на момент entry
- compare_good_vs_bad.csv — агрегована таблиця good (09-11) vs bad (12-16)
- hour_breakdown.csv     — розподіл по годинах UTC
- symbol_breakdown.csv   — розподіл по символах
- hypothesis_report.csv  — зведення H1..H4
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import cast

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

# ---------- Paths ----------
ROOT = Path("D:/Trading/newFlo")
LIVE_DIR = ROOT / "liveLoad"
DATA_DIR = ROOT / "live_data"
OUT_DIR = ROOT / "reports" / "regime_apr2026"
OUT_DIR.mkdir(parents=True, exist_ok=True)

SYMBOLS = ["SOLUSDT", "SUIUSDT", "DOGEUSDT", "XRPUSDT", "ETHUSDT"]
GOOD_DAYS = ["2026-04-09", "2026-04-10", "2026-04-11"]
BAD_DAYS = ["2026-04-12", "2026-04-13", "2026-04-14", "2026-04-15", "2026-04-16"]


# ---------- Helpers ----------
def load_parquet(sym: str, tf: str) -> pd.DataFrame:
    path = DATA_DIR / sym / f"{tf}_apr2026.parquet"
    df = pq.read_table(path).to_pandas()
    df["ts"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    df["date"] = df["ts"].dt.strftime("%Y-%m-%d")
    df["symbol"] = sym
    df = df.sort_values("ts").reset_index(drop=True)
    return df


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return cast(pd.Series, tr.rolling(n, min_periods=n).mean())


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    up = h.diff()
    dn = -l.diff()
    plus_dm = ((up > dn) & (up > 0)) * up
    minus_dm = ((dn > up) & (dn > 0)) * dn
    tr = pd.concat([(h - l), (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr_n = tr.rolling(n, min_periods=n).mean()
    plus_di = 100 * (plus_dm.rolling(n, min_periods=n).mean() / atr_n)
    minus_di = 100 * (minus_dm.rolling(n, min_periods=n).mean() / atr_n)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.rolling(n, min_periods=n).mean()


def daily_regime_metrics(df5: pd.DataFrame, df1: pd.DataFrame) -> pd.DataFrame:
    """Щоденні метрики на 5m + volatility_1m."""
    df5 = df5.copy()
    df5["atr14"] = atr(df5, 14)
    df5["adx14"] = adx(df5, 14)
    df5["range"] = df5["high"] - df5["low"]
    df5["range_rel"] = df5["range"] / df5["close"]

    df1 = df1.copy()
    df1["logret"] = np.log(df1["close"] / df1["close"].shift(1))

    rows = []
    for date, _g5 in df5.groupby("date"):
        g5 = cast(pd.DataFrame, _g5)
        g1 = cast(pd.DataFrame, df1[df1["date"] == date])
        if g5.empty or g1.empty:
            continue
        atr_mean = float(cast(pd.Series, g5["atr14"]).mean(skipna=True))
        adx_mean = float(cast(pd.Series, g5["adx14"]).mean(skipna=True))
        range_compression = float(cast(pd.Series, g5["range_rel"]).median())
        volatility_1m = float(np.std(cast(pd.Series, g1["logret"]).to_numpy(), ddof=1)) * math.sqrt(1440)
        close_start = float(cast(pd.Series, g5["close"]).iloc[0])
        close_end = float(cast(pd.Series, g5["close"]).iloc[-1])
        n_bars = len(g5)
        # trend_strength: |net move| / (ATR * sqrt(N))  — нормалізований drift
        denom = atr_mean * math.sqrt(max(n_bars, 1)) if atr_mean and atr_mean > 0 else float("nan")
        trend_strength = abs(close_end - close_start) / denom if denom else float("nan")
        vol_mean = float(cast(pd.Series, g5["volume"]).mean())
        rows.append(
            {
                "symbol": str(cast(pd.Series, g5["symbol"]).iloc[0]),
                "date": date,
                "atr_mean_5m": atr_mean,
                "adx_mean_5m": adx_mean,
                "range_compression": range_compression,
                "volatility_1m": volatility_1m,
                "trend_strength": trend_strength,
                "close_start": close_start,
                "close_end": close_end,
                "daily_return": (close_end / close_start - 1),
                "volume_mean": vol_mean,
            }
        )
    out = pd.DataFrame(rows).sort_values(["symbol", "date"]).reset_index(drop=True)
    # 7-денні rolling референси (по символу). Вікна малі (8 днів даних) — використовуємо min_periods=3.
    out["atr_7d_median"] = out.groupby("symbol")["atr_mean_5m"].transform(
        lambda s: s.rolling(7, min_periods=3).median()
    )
    out["atr_rank"] = out.groupby("symbol")["atr_mean_5m"].transform(
        lambda s: s.rank(pct=True)
    )
    out["volume_7d_mean"] = out.groupby("symbol")["volume_mean"].transform(
        lambda s: s.rolling(7, min_periods=3).mean()
    )
    out["volume_7d_std"] = out.groupby("symbol")["volume_mean"].transform(
        lambda s: s.rolling(7, min_periods=3).std()
    )
    out["volume_zscore"] = (out["volume_mean"] - out["volume_7d_mean"]) / out["volume_7d_std"]
    return out


def load_trades() -> pd.DataFrame:
    files = sorted(LIVE_DIR.glob("trades_202604*.csv"))
    frames = [pd.read_csv(f) for f in files]
    df = pd.concat(frames, ignore_index=True)
    df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True)
    df["exit_time"] = pd.to_datetime(df["exit_time"], utc=True)
    df["date"] = df["entry_time"].dt.strftime("%Y-%m-%d")
    df["hour_utc"] = df["entry_time"].dt.hour
    df["gross_pnl"] = pd.to_numeric(df["gross_pnl"], errors="coerce")
    df["fees"] = pd.to_numeric(df["fees"], errors="coerce")
    df["slippage_estimate"] = pd.to_numeric(df["slippage_estimate"], errors="coerce")
    df["net_pnl"] = pd.to_numeric(df["net_pnl"], errors="coerce")
    df["win"] = (df["net_pnl"] > 0).astype(int)
    df["cost_drag_ratio"] = np.where(
        df["gross_pnl"].abs() > 0,
        (df["fees"] + df["slippage_estimate"]) / df["gross_pnl"].abs(),
        np.nan,
    )
    df["group"] = np.where(df["date"].isin(GOOD_DAYS), "good", "bad")
    return df


def enrich_trades(trades: pd.DataFrame, daily_all: pd.DataFrame, bars5_all: dict) -> pd.DataFrame:
    """Додає regime-контекст на момент entry для кожного трейда."""
    rows = []
    for _, tr in trades.iterrows():
        sym = tr["symbol"]
        d = tr["date"]
        reg = daily_all[(daily_all["symbol"] == sym) & (daily_all["date"] == d)]
        reg_row = reg.iloc[0].to_dict() if not reg.empty else {}
        bars5 = bars5_all[sym]
        # ATR на момент entry
        entry_ts = tr["entry_time"]
        exit_ts = tr["exit_time"]
        mask_entry = bars5["ts"] <= entry_ts
        atr_at_entry = bars5.loc[mask_entry, "atr14"].iloc[-1] if mask_entry.any() else np.nan
        close_at_entry = bars5.loc[mask_entry, "close"].iloc[-1] if mask_entry.any() else np.nan
        # Діапазон ATR пройдений за hold
        hold_bars = bars5[(bars5["ts"] >= entry_ts) & (bars5["ts"] <= exit_ts)]
        if len(hold_bars) >= 1 and atr_at_entry and atr_at_entry > 0:
            move = hold_bars["close"].iloc[-1] - hold_bars["close"].iloc[0]
            atrs_traveled = abs(move) / atr_at_entry
        else:
            atrs_traveled = np.nan
        hold_5m = tr["hold_time_seconds"] / 300.0
        rows.append(
            {
                **tr.to_dict(),
                "atr_at_entry_5m": atr_at_entry,
                "close_at_entry": close_at_entry,
                "atr_rank_day": reg_row.get("atr_rank", np.nan),
                "adx_day": reg_row.get("adx_mean_5m", np.nan),
                "range_compression_day": reg_row.get("range_compression", np.nan),
                "trend_strength_day": reg_row.get("trend_strength", np.nan),
                "volume_zscore_day": reg_row.get("volume_zscore", np.nan),
                "daily_return_sym": reg_row.get("daily_return", np.nan),
                "atrs_traveled_in_hold": atrs_traveled,
                "hold_bars_5m": hold_5m,
            }
        )
    return pd.DataFrame(rows)


def summarize_groups(enriched: pd.DataFrame) -> pd.DataFrame:
    """good vs bad — ключові метрики."""
    def col(df: pd.DataFrame, name: str) -> pd.Series:
        return cast(pd.Series, df[name])

    def agg(df: pd.DataFrame) -> dict:
        if df.empty:
            return {}
        gross = float(col(df, "gross_pnl").sum())
        net = float(col(df, "net_pnl").sum())
        wins = int(col(df, "win").sum())
        losses = len(df) - wins
        cost_total = float(col(df, "fees").sum()) + float(col(df, "slippage_estimate").sum())
        return {
            "trades": len(df),
            "wins": wins,
            "losses": losses,
            "win_rate": wins / len(df),
            "gross_pnl": gross,
            "net_pnl": net,
            "total_cost": cost_total,
            "cost_to_gross_ratio": cost_total / max(abs(gross), 1e-9),
            "avg_hold_min": float(col(df, "hold_time_seconds").mean()) / 60,
            "median_hold_min": float(col(df, "hold_time_seconds").median()) / 60,
            "avg_atr_rank_entry": float(col(df, "atr_rank_day").mean()),
            "avg_adx_day": float(col(df, "adx_day").mean()),
            "avg_range_compression": float(col(df, "range_compression_day").mean()),
            "avg_trend_strength": float(col(df, "trend_strength_day").mean()),
            "avg_volume_z": float(col(df, "volume_zscore_day").mean()),
            "avg_atrs_traveled_in_hold": float(col(df, "atrs_traveled_in_hold").mean()),
            "pct_sui": float((col(df, "symbol") == "SUIUSDT").mean()),
            "pct_tp_exit": float((col(df, "exit_reason") == "tp").mean()),
        }

    good = cast(pd.DataFrame, enriched[enriched["group"] == "good"])
    bad = cast(pd.DataFrame, enriched[enriched["group"] == "bad"])
    rec_good = agg(good)
    rec_bad = agg(bad)
    keys = sorted(set(rec_good) | set(rec_bad))
    rows = [
        {"metric": k, "good_09_11": rec_good.get(k), "bad_12_16": rec_bad.get(k)}
        for k in keys
    ]
    return pd.DataFrame(rows)


def hour_breakdown(enriched: pd.DataFrame) -> pd.DataFrame:
    g = enriched.groupby(["group", "hour_utc"]).agg(
        trades=("trade_id", "count"),
        wins=("win", "sum"),
        net_pnl=("net_pnl", "sum"),
    ).reset_index()
    g["win_rate"] = g["wins"] / g["trades"]
    return g


def symbol_breakdown(enriched: pd.DataFrame) -> pd.DataFrame:
    g = enriched.groupby(["group", "symbol"]).agg(
        trades=("trade_id", "count"),
        wins=("win", "sum"),
        net_pnl=("net_pnl", "sum"),
        avg_atr_rank=("atr_rank_day", "mean"),
        avg_range_compression=("range_compression_day", "mean"),
        avg_atrs_traveled=("atrs_traveled_in_hold", "mean"),
    ).reset_index()
    g["win_rate"] = g["wins"] / g["trades"]
    return g


def hypothesis_tests(enriched: pd.DataFrame, daily_all: pd.DataFrame) -> pd.DataFrame:
    recs = []

    # H1: regime shift (low-vol chop після 11.04). Агрегат по всіх 5 символах.
    # Беремо середні регіон-метрики good vs bad days на рівні (symbol, date).
    good_reg = daily_all[daily_all["date"].isin(GOOD_DAYS)]
    bad_reg = daily_all[daily_all["date"].isin(BAD_DAYS)]
    h1 = {
        "hypothesis": "H1_regime_shift_low_vol_chop",
        "good_atr_mean_5m": good_reg["atr_mean_5m"].mean() / good_reg["close_end"].mean(),
        "bad_atr_mean_5m": bad_reg["atr_mean_5m"].mean() / bad_reg["close_end"].mean(),
        "good_range_compression": good_reg["range_compression"].mean(),
        "bad_range_compression": bad_reg["range_compression"].mean(),
        "good_adx_mean": good_reg["adx_mean_5m"].mean(),
        "bad_adx_mean": bad_reg["adx_mean_5m"].mean(),
        "good_trend_strength": good_reg["trend_strength"].mean(),
        "bad_trend_strength": bad_reg["trend_strength"].mean(),
    }
    recs.append(h1)

    # H2: SUI concentration + зміна режиму SUI
    sui_good = daily_all[(daily_all["symbol"] == "SUIUSDT") & (daily_all["date"].isin(GOOD_DAYS))]
    sui_bad = daily_all[(daily_all["symbol"] == "SUIUSDT") & (daily_all["date"].isin(BAD_DAYS))]
    pct_sui_good = (enriched[enriched["group"] == "good"]["symbol"] == "SUIUSDT").mean()
    pct_sui_bad = (enriched[enriched["group"] == "bad"]["symbol"] == "SUIUSDT").mean()
    h2 = {
        "hypothesis": "H2_sui_concentration_and_regime",
        "pct_sui_good": pct_sui_good,
        "pct_sui_bad": pct_sui_bad,
        "sui_atr_good": sui_good["atr_mean_5m"].mean(),
        "sui_atr_bad": sui_bad["atr_mean_5m"].mean(),
        "sui_adx_good": sui_good["adx_mean_5m"].mean(),
        "sui_adx_bad": sui_bad["adx_mean_5m"].mean(),
        "sui_range_compression_good": sui_good["range_compression"].mean(),
        "sui_range_compression_bad": sui_bad["range_compression"].mean(),
        "sui_trend_strength_good": sui_good["trend_strength"].mean(),
        "sui_trend_strength_bad": sui_bad["trend_strength"].mean(),
    }
    recs.append(h2)

    # H3: stale signals — довгий hold без руху ATR
    h3 = {
        "hypothesis": "H3_stale_signals",
        "good_avg_hold_min": enriched[enriched["group"] == "good"]["hold_time_seconds"].mean() / 60,
        "bad_avg_hold_min": enriched[enriched["group"] == "bad"]["hold_time_seconds"].mean() / 60,
        "good_avg_atrs_traveled": enriched[enriched["group"] == "good"]["atrs_traveled_in_hold"].mean(),
        "bad_avg_atrs_traveled": enriched[enriched["group"] == "bad"]["atrs_traveled_in_hold"].mean(),
        "pct_trades_low_motion_good": ((enriched[enriched["group"] == "good"]["atrs_traveled_in_hold"] < 1.0).mean()),
        "pct_trades_low_motion_bad": ((enriched[enriched["group"] == "bad"]["atrs_traveled_in_hold"] < 1.0).mean()),
    }
    recs.append(h3)

    # H4: cost drag > 15% gross корелює зі збитковими днями
    # Для кожного трейда: loss_flag = 1 якщо net<=0, cost_flag=1 якщо (fees+slip)/|gross|>0.15
    e = enriched.copy()
    e["loss"] = (cast(pd.Series, e["net_pnl"]) <= 0).astype(int)
    e["heavy_cost"] = (
        (cast(pd.Series, e["fees"]) + cast(pd.Series, e["slippage_estimate"]))
        / cast(pd.Series, e["gross_pnl"]).abs().replace(0, np.nan)
        > 0.15
    ).astype(int)
    corr = float(np.corrcoef(e["loss"].to_numpy(), e["heavy_cost"].to_numpy())[0, 1])
    good_df = cast(pd.DataFrame, enriched[enriched["group"] == "good"])
    bad_df = cast(pd.DataFrame, enriched[enriched["group"] == "bad"])
    h4 = {
        "hypothesis": "H4_cost_drag_vs_loss",
        "good_cost_ratio": (float(cast(pd.Series, good_df["fees"]).sum())
                             + float(cast(pd.Series, good_df["slippage_estimate"]).sum()))
                            / max(float(cast(pd.Series, good_df["gross_pnl"]).abs().sum()), 1e-9),
        "bad_cost_ratio": (float(cast(pd.Series, bad_df["fees"]).sum())
                            + float(cast(pd.Series, bad_df["slippage_estimate"]).sum()))
                           / max(float(cast(pd.Series, bad_df["gross_pnl"]).abs().sum()), 1e-9),
        "pct_trades_heavy_cost_good": (enriched[enriched["group"] == "good"]
                                       .assign(hc=lambda d: (d["fees"] + d["slippage_estimate"]) / d["gross_pnl"].abs().replace(0, np.nan) > 0.15)
                                       ["hc"].mean()),
        "pct_trades_heavy_cost_bad": (enriched[enriched["group"] == "bad"]
                                      .assign(hc=lambda d: (d["fees"] + d["slippage_estimate"]) / d["gross_pnl"].abs().replace(0, np.nan) > 0.15)
                                      ["hc"].mean()),
        "corr_loss_heavycost": corr,
    }
    recs.append(h4)

    # Render вертикально
    return pd.DataFrame(recs)


# ---------- Main ----------
def main():
    print("[1/5] Load candles & compute daily regime...")
    bars5_all: dict[str, pd.DataFrame] = {}
    bars1_all: dict[str, pd.DataFrame] = {}
    daily_frames = []
    for sym in SYMBOLS:
        b5 = load_parquet(sym, "5m")
        b1 = load_parquet(sym, "1m")
        b5["atr14"] = atr(b5, 14)
        b5["adx14"] = adx(b5, 14)
        bars5_all[sym] = b5
        bars1_all[sym] = b1
        daily = daily_regime_metrics(b5, b1)
        daily_frames.append(daily)
    daily_all = pd.concat(daily_frames, ignore_index=True)
    daily_all.to_csv(OUT_DIR / "regime_daily.csv", index=False)
    print(f"  -> regime_daily.csv ({len(daily_all)} rows)")

    print("[2/5] Load live trades...")
    trades = load_trades()
    print(f"  -> {len(trades)} trades | net sum={trades['net_pnl'].sum():.3f} | WR={trades['win'].mean():.3f}")

    print("[3/5] Enrich trades with regime context at entry...")
    enriched = enrich_trades(trades, daily_all, bars5_all)
    enriched.to_csv(OUT_DIR / "trades_enriched.csv", index=False)

    print("[4/5] Build breakdowns...")
    compare = summarize_groups(enriched)
    compare.to_csv(OUT_DIR / "compare_good_vs_bad.csv", index=False)
    hour_breakdown(enriched).to_csv(OUT_DIR / "hour_breakdown.csv", index=False)
    symbol_breakdown(enriched).to_csv(OUT_DIR / "symbol_breakdown.csv", index=False)

    print("[5/5] Run hypothesis tests...")
    hypo = hypothesis_tests(enriched, daily_all)
    hypo.to_csv(OUT_DIR / "hypothesis_report.csv", index=False)

    # --- Print key tables ---
    pd.set_option("display.max_columns", 40)
    pd.set_option("display.width", 220)
    pd.set_option("display.float_format", lambda x: f"{x:.5f}")
    print("\n=== DAILY REGIME (head) ===")
    print(daily_all.head(15).to_string(index=False))
    print("\n=== GOOD vs BAD ===")
    print(compare.to_string(index=False))
    print("\n=== SYMBOL BREAKDOWN ===")
    print(symbol_breakdown(enriched).to_string(index=False))
    print("\n=== HOUR BREAKDOWN ===")
    print(hour_breakdown(enriched).to_string(index=False))
    print("\n=== HYPOTHESES ===")
    for _, row in hypo.iterrows():
        print(f"\n-- {row['hypothesis']}")
        for k, v in row.items():
            if k == "hypothesis":
                continue
            print(f"   {k}: {v}")

    # Розподіл ATR rank по всіх трейдах (корисно для thresholds)
    print("\n=== ATR RANK AT ENTRY: quantiles ===")
    print(enriched["atr_rank_day"].describe())

    print("\nDone. Output in:", OUT_DIR)


if __name__ == "__main__":
    main()
