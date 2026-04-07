"""Walk-forward evaluation runner with OOS gates and side-bias policy."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import tempfile
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import product
from pathlib import Path
from statistics import median
from typing import Any

import yaml


@dataclass
class SplitWindow:
    train_from: date
    train_to: date
    valid_from: date
    valid_to: date


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def fmt_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def parse_num(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return 0.0
    s = s.replace("%", "").replace(",", ".")
    return float(s)


def pct_to_float(value: Any) -> float:
    return parse_num(value)


def compute_pf_from_pnls(pnls: list[float]) -> float:
    gross_profit = sum(x for x in pnls if x > 0)
    gross_loss = -sum(x for x in pnls if x < 0)
    if gross_loss <= 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def load_base_config(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def list_report_dirs() -> set[Path]:
    base = Path("reports")
    if not base.exists():
        return set()
    return {p for p in base.iterdir() if p.is_dir()}


def latest_report_dir(after: set[Path]) -> Path | None:
    current = list_report_dirs()
    created = [p for p in current if p not in after]
    candidates = created if created else list(current)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def read_metrics(metrics_path: Path) -> dict[str, Any]:
    with open(metrics_path, encoding="utf-8") as f:
        return json.load(f)


def read_trades(trades_path: Path) -> list[dict[str, Any]]:
    if not trades_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with open(trades_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def side_stats(trades: list[dict[str, Any]], side: str) -> tuple[int, float]:
    side_rows = [r for r in trades if r.get("side", "").lower() == side]
    count = len(side_rows)
    pnls = [parse_num(r.get("net_pnl", "0")) for r in side_rows]
    return count, compute_pf_from_pnls(pnls)


def build_splits(start: date, end: date, splits: int) -> list[SplitWindow]:
    # Planned schedule for this repo/time range.
    if start == date(2026, 2, 3) and end == date(2026, 2, 10) and splits >= 3:
        preset = [
            SplitWindow(date(2026, 2, 3), date(2026, 2, 5), date(2026, 2, 6), date(2026, 2, 6)),
            SplitWindow(date(2026, 2, 4), date(2026, 2, 6), date(2026, 2, 7), date(2026, 2, 7)),
            SplitWindow(date(2026, 2, 6), date(2026, 2, 8), date(2026, 2, 9), date(2026, 2, 9)),
        ]
        return preset[:splits]

    # Generic fallback: rolling 3-day train + 1-day validation, step=1 day.
    out: list[SplitWindow] = []
    cur = start
    while len(out) < splits:
        train_from = cur
        train_to = cur + timedelta(days=2)
        valid_from = train_to + timedelta(days=1)
        valid_to = valid_from
        if valid_to > end:
            break
        out.append(SplitWindow(train_from, train_to, valid_from, valid_to))
        cur += timedelta(days=1)
    return out


def candidate_space() -> dict[str, list[Any]]:
    return {
        "imbalance_threshold": [0.20, 0.25, 0.30, 0.35],
        "delta_threshold": [0.08, 0.10, 0.12, 0.15],
        "atr_multiplier": [2.25, 2.5, 2.75, 3.0],
        "rr_ratio": [2.5, 3.0, 3.5],
        "trend_candles": [3, 4],
        "min_atr_pct": [0.0008, 0.0010, 0.0012],
        "m1_confirm_candles": [2, 3, 4],
        "cooldown_seconds": [60, 120, 180],
        "max_position_pct": [1.0, 1.5, 2.0],
        "max_daily_sl_count": [2, 3],
        "max_concurrent_trades": [1, 2, 3],
    }


def build_candidates(max_candidates: int) -> list[dict[str, Any]]:
    space = candidate_space()
    keys = list(space.keys())
    values = [space[k] for k in keys]
    out: list[dict[str, Any]] = []
    for combo in product(*values):
        candidate = {k: v for k, v in zip(keys, combo, strict=False)}
        out.append(candidate)
        if len(out) >= max_candidates:
            break
    return out


def apply_overrides(
    base_cfg: dict[str, Any],
    params: dict[str, Any],
    start_date: date,
    end_date: date,
    allow_long: bool,
    allow_short: bool,
    limit_symbols: list[str] | None = None,
) -> dict[str, Any]:
    cfg = deepcopy(base_cfg)
    cfg["mode"] = "backtest"
    cfg.setdefault("strategy", {}).setdefault("params", {})
    cfg.setdefault("risk", {})
    cfg.setdefault("backtest", {})

    for key in (
        "imbalance_threshold",
        "delta_threshold",
        "atr_multiplier",
        "rr_ratio",
        "trend_candles",
        "min_atr_pct",
        "m1_confirm_candles",
        "cooldown_seconds",
    ):
        if key in params:
            cfg["strategy"]["params"][key] = params[key]

    for key in ("max_position_pct", "max_daily_sl_count", "max_concurrent_trades"):
        if key in params:
            cfg["risk"][key] = params[key]

    cfg["strategy"]["params"]["allow_long"] = allow_long
    cfg["strategy"]["params"]["allow_short"] = allow_short
    cfg["backtest"]["start_date"] = fmt_date(start_date)
    cfg["backtest"]["end_date"] = fmt_date(end_date)
    if limit_symbols:
        cfg.setdefault("symbols", {})
        cfg["symbols"]["trade"] = limit_symbols
        cfg["symbols"]["record"] = []
        cfg["backtest"]["symbols"] = limit_symbols
    return cfg


def run_backtest_with_config(
    cfg: dict[str, Any],
    python_bin: str,
    stream_output: bool = False,
) -> dict[str, Any]:
    before = list_report_dirs()

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as tmp:
        yaml.safe_dump(cfg, tmp, sort_keys=False)
        temp_path = Path(tmp.name)

    try:
        if stream_output:
            proc = subprocess.run(
                [python_bin, "-m", "app", "backtest", "-c", str(temp_path), "--log-level", "INFO"],
            )
        else:
            proc = subprocess.run(
                [python_bin, "-m", "app", "backtest", "-c", str(temp_path), "--log-level", "WARNING"],
                capture_output=True,
                text=True,
            )
    finally:
        temp_path.unlink(missing_ok=True)

    if proc.returncode != 0:
        return {
            "ok": False,
            "error": proc.stderr[-2000:] if proc.stderr else proc.stdout[-2000:],
        }

    report_dir = latest_report_dir(before)
    if report_dir is None:
        return {"ok": False, "error": "No report directory found after backtest"}

    metrics_path = report_dir / "metrics.json"
    trades_path = report_dir / "trades.csv"
    if not metrics_path.exists():
        return {"ok": False, "error": f"Missing metrics.json in {report_dir}"}

    metrics = read_metrics(metrics_path)
    trades = read_trades(trades_path)
    return {
        "ok": True,
        "report_dir": str(report_dir),
        "metrics": metrics,
        "trades": trades,
    }


def decide_side_policy(
    train_trades: list[dict[str, Any]],
    base_cfg: dict[str, Any],
) -> tuple[bool, bool, dict[str, Any]]:
    params = base_cfg.get("strategy", {}).get("params", {})
    min_trades = int(params.get("bias_min_trades", 15))
    min_pf = float(params.get("bias_min_pf", 1.10))

    long_count, long_pf = side_stats(train_trades, "buy")
    short_count, short_pf = side_stats(train_trades, "sell")

    allow_long = True if long_count < min_trades else long_pf >= min_pf
    allow_short = True if short_count < min_trades else short_pf >= min_pf

    if not allow_long and not allow_short:
        # Fallback: keep stronger side enabled.
        if long_pf >= short_pf:
            allow_long = True
        else:
            allow_short = True

    meta = {
        "bias_min_trades": min_trades,
        "bias_min_pf": min_pf,
        "long_count": long_count,
        "long_pf": long_pf,
        "short_count": short_count,
        "short_pf": short_pf,
    }
    return allow_long, allow_short, meta


def evaluate_candidate(
    base_cfg: dict[str, Any],
    candidate: dict[str, Any],
    splits: list[SplitWindow],
    dd_cap: float,
    oos_daily_target_pct: float,
    python_bin: str,
    limit_symbols: list[str] | None,
    stream_output: bool,
) -> dict[str, Any]:
    split_results: list[dict[str, Any]] = []
    oos_pnls: list[float] = []
    oos_trade_count = 0
    fail_fast = False
    fail_fast_reasons: list[str] = []
    max_dd_seen = 0.0
    daily_returns: list[float] = []
    last_policy = {"allow_long": True, "allow_short": True}

    for idx, split in enumerate(splits, start=1):
        print(
            f"  split={idx} train={fmt_date(split.train_from)}..{fmt_date(split.train_to)} "
            f"valid={fmt_date(split.valid_from)}..{fmt_date(split.valid_to)}"
        )
        train_t0 = time.time()
        train_cfg = apply_overrides(
            base_cfg=base_cfg,
            params=candidate,
            start_date=split.train_from,
            end_date=split.train_to,
            allow_long=True,
            allow_short=True,
            limit_symbols=limit_symbols,
        )
        train_run = run_backtest_with_config(
            train_cfg,
            python_bin=python_bin,
            stream_output=stream_output,
        )
        if not train_run["ok"]:
            return {
                "params": candidate,
                "pass": False,
                "error": f"Train split {idx} failed: {train_run['error']}",
                "splits": [],
            }

        print(f"    train done in {time.time() - train_t0:.1f}s -> {train_run['report_dir']}")

        allow_long, allow_short, bias_meta = decide_side_policy(train_run["trades"], base_cfg)
        last_policy = {"allow_long": allow_long, "allow_short": allow_short}

        valid_t0 = time.time()
        valid_cfg = apply_overrides(
            base_cfg=base_cfg,
            params=candidate,
            start_date=split.valid_from,
            end_date=split.valid_to,
            allow_long=allow_long,
            allow_short=allow_short,
            limit_symbols=limit_symbols,
        )
        valid_run = run_backtest_with_config(
            valid_cfg,
            python_bin=python_bin,
            stream_output=stream_output,
        )
        if not valid_run["ok"]:
            return {
                "params": candidate,
                "pass": False,
                "error": f"Valid split {idx} failed: {valid_run['error']}",
                "splits": [],
            }

        print(f"    valid done in {time.time() - valid_t0:.1f}s -> {valid_run['report_dir']}")

        m = valid_run["metrics"]
        pf = parse_num(m.get("profit_factor", 0))
        dd = pct_to_float(m.get("max_drawdown_pct", "0%"))
        trades = int(m.get("total_trades", 0))
        ret = pct_to_float(m.get("return_pct", "0%"))
        valid_days = (split.valid_to - split.valid_from).days + 1
        daily_ret = ret / max(valid_days, 1)

        split_pnls = [parse_num(r.get("net_pnl", "0")) for r in valid_run["trades"]]
        oos_pnls.extend(split_pnls)
        oos_trade_count += trades
        max_dd_seen = max(max_dd_seen, dd)
        daily_returns.append(daily_ret)

        if trades >= 10 and pf < 1.0:
            fail_fast = True
            fail_fast_reasons.append(f"split_{idx}: pf<1.0 with trades={trades}")
        if dd > dd_cap:
            fail_fast = True
            fail_fast_reasons.append(f"split_{idx}: dd={dd:.2f}% > cap={dd_cap:.2f}%")

        split_results.append(
            {
                "split": idx,
                "train_from": fmt_date(split.train_from),
                "train_to": fmt_date(split.train_to),
                "valid_from": fmt_date(split.valid_from),
                "valid_to": fmt_date(split.valid_to),
                "allow_long": allow_long,
                "allow_short": allow_short,
                "bias_meta": bias_meta,
                "train_report_dir": train_run["report_dir"],
                "valid_report_dir": valid_run["report_dir"],
                "valid_metrics": m,
            }
        )

    pfs = [parse_num(s["valid_metrics"].get("profit_factor", 0)) for s in split_results]
    median_pf = median(pfs) if pfs else 0.0
    aggregate_pf = compute_pf_from_pnls(oos_pnls)
    avg_daily_ret = sum(daily_returns) / len(daily_returns) if daily_returns else 0.0

    gate_reasons: list[str] = []
    passes = True
    if median_pf < 1.35:
        passes = False
        gate_reasons.append(f"median_pf={median_pf:.2f} < 1.35")
    if aggregate_pf < 1.5:
        passes = False
        gate_reasons.append(f"aggregate_pf={aggregate_pf:.2f} < 1.50")
    if max_dd_seen > dd_cap:
        passes = False
        gate_reasons.append(f"max_dd={max_dd_seen:.2f}% > {dd_cap:.2f}%")
    if oos_trade_count < 30:
        passes = False
        gate_reasons.append(f"oos_trades={oos_trade_count} < 30")
    if avg_daily_ret < oos_daily_target_pct:
        passes = False
        gate_reasons.append(f"avg_daily_return={avg_daily_ret:.2f}% < {oos_daily_target_pct:.2f}%")

    if fail_fast:
        passes = False
        gate_reasons.extend(fail_fast_reasons)

    return {
        "params": candidate,
        "pass": passes,
        "gate_reasons": gate_reasons,
        "median_pf": median_pf,
        "aggregate_pf": aggregate_pf,
        "max_dd_pct": max_dd_seen,
        "oos_trade_count": oos_trade_count,
        "avg_daily_return_pct": avg_daily_ret,
        "recommended_allow_long": last_policy["allow_long"],
        "recommended_allow_short": last_policy["allow_short"],
        "splits": split_results,
    }


def write_outputs(results: list[dict[str, Any]], splits: list[SplitWindow]) -> Path:
    run_id = datetime.utcnow().strftime("wfo_%Y%m%d_%H%M%S")
    out_dir = Path("reports") / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    sorted_results = sorted(
        results,
        key=lambda r: (
            0 if r.get("pass") else 1,
            -float(r.get("aggregate_pf", 0)),
            -float(r.get("avg_daily_return_pct", 0)),
        ),
    )

    leaderboard_path = out_dir / "leaderboard.csv"
    with open(leaderboard_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "rank",
                "pass",
                "aggregate_pf",
                "median_pf",
                "avg_daily_return_pct",
                "max_dd_pct",
                "oos_trade_count",
                "recommended_allow_long",
                "recommended_allow_short",
                "params_json",
                "gate_reasons",
            ],
        )
        writer.writeheader()
        for idx, row in enumerate(sorted_results, start=1):
            writer.writerow(
                {
                    "rank": idx,
                    "pass": str(bool(row.get("pass"))).lower(),
                    "aggregate_pf": f"{float(row.get('aggregate_pf', 0)):.4f}",
                    "median_pf": f"{float(row.get('median_pf', 0)):.4f}",
                    "avg_daily_return_pct": f"{float(row.get('avg_daily_return_pct', 0)):.4f}",
                    "max_dd_pct": f"{float(row.get('max_dd_pct', 0)):.4f}",
                    "oos_trade_count": int(row.get("oos_trade_count", 0)),
                    "recommended_allow_long": str(
                        bool(row.get("recommended_allow_long", True))
                    ).lower(),
                    "recommended_allow_short": str(
                        bool(row.get("recommended_allow_short", True))
                    ).lower(),
                    "params_json": json.dumps(row.get("params", {}), ensure_ascii=True),
                    "gate_reasons": " | ".join(row.get("gate_reasons", [])),
                }
            )

    summary = {
        "generated_at": datetime.utcnow().isoformat(),
        "splits": [
            {
                "train_from": fmt_date(s.train_from),
                "train_to": fmt_date(s.train_to),
                "valid_from": fmt_date(s.valid_from),
                "valid_to": fmt_date(s.valid_to),
            }
            for s in splits
        ],
        "results": sorted_results,
    }
    with open(out_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    return out_dir


def parse_best_from_leaderboard(path: Path) -> dict[str, Any]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    if not rows:
        raise ValueError(f"Leaderboard is empty: {path}")

    def row_key(r: dict[str, str]) -> tuple[int, float, float]:
        passed = 0 if r.get("pass", "false").lower() == "true" else 1
        pf = -parse_num(r.get("aggregate_pf", "0"))
        ret = -parse_num(r.get("avg_daily_return_pct", "0"))
        return passed, pf, ret

    best = sorted(rows, key=row_key)[0]
    params = json.loads(best["params_json"])
    return {
        "params": params,
        "allow_long": best.get("recommended_allow_long", "true").lower() == "true",
        "allow_short": best.get("recommended_allow_short", "true").lower() == "true",
    }


def run_holdout(
    base_cfg: dict[str, Any],
    best_from: Path,
    holdout_from: date,
    holdout_to: date,
    python_bin: str,
) -> int:
    best = parse_best_from_leaderboard(best_from)
    cfg = apply_overrides(
        base_cfg=base_cfg,
        params=best["params"],
        start_date=holdout_from,
        end_date=holdout_to,
        allow_long=best["allow_long"],
        allow_short=best["allow_short"],
    )
    result = run_backtest_with_config(cfg, python_bin=python_bin)
    if not result["ok"]:
        print(f"[HOLDOUT] failed: {result['error']}")
        return 1

    metrics = result["metrics"]
    print("[HOLDOUT] complete")
    print(f"report_dir={result['report_dir']}")
    print(
        f"trades={metrics.get('total_trades', 0)}, pf={metrics.get('profit_factor', '0')}, "
        f"return={metrics.get('return_pct', '0%')}, max_dd={metrics.get('max_drawdown_pct', '0%')}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Walk-forward OOS evaluation with gate rules.")
    parser.add_argument("--config", required=True, help="Path to base config.yaml")
    parser.add_argument("--from", dest="start_date", help="Walk-forward start date YYYY-MM-DD")
    parser.add_argument("--to", dest="end_date", help="Walk-forward end date YYYY-MM-DD")
    parser.add_argument(
        "--splits",
        type=int,
        default=3,
        help="Number of walk-forward splits",
    )
    parser.add_argument(
        "--dd-cap",
        type=float,
        default=20.0,
        help="Max drawdown cap in percent",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=24,
        help="Number of candidates to evaluate",
    )
    parser.add_argument(
        "--oos-daily-target-pct",
        type=float,
        default=0.47,
        help="Target average OOS daily return (%%)",
    )
    parser.add_argument(
        "--python-bin",
        default=sys.executable,
        help="Python interpreter to run backtests",
    )
    parser.add_argument(
        "--holdout-from",
        dest="holdout_from",
        help="Holdout start date YYYY-MM-DD",
    )
    parser.add_argument(
        "--holdout-to",
        dest="holdout_to",
        help="Holdout end date YYYY-MM-DD",
    )
    parser.add_argument(
        "--best-from",
        dest="best_from",
        help="Leaderboard CSV from previous WFO run",
    )
    parser.add_argument(
        "--limit-symbols",
        default="",
        help="Comma-separated symbols for faster evaluation, e.g. SOLUSDT or SOLUSDT,SUIUSDT",
    )
    parser.add_argument(
        "--stream-backtest-output",
        action="store_true",
        help="Stream child backtest logs (slower output, better visibility)",
    )
    args = parser.parse_args()

    base_cfg = load_base_config(Path(args.config))
    limit_symbols = [s.strip() for s in args.limit_symbols.split(",") if s.strip()] or None

    if args.holdout_from or args.holdout_to or args.best_from:
        if not (args.holdout_from and args.holdout_to and args.best_from):
            print("Holdout mode requires --holdout-from, --holdout-to and --best-from")
            return 2
        return run_holdout(
            base_cfg=base_cfg,
            best_from=Path(args.best_from),
            holdout_from=parse_date(args.holdout_from),
            holdout_to=parse_date(args.holdout_to),
            python_bin=args.python_bin,
        )

    if not args.start_date or not args.end_date:
        print("Walk-forward mode requires --from and --to")
        return 2

    start = parse_date(args.start_date)
    end = parse_date(args.end_date)
    splits = build_splits(start, end, args.splits)
    if len(splits) < args.splits:
        print(f"Warning: requested splits={args.splits}, available={len(splits)} for date range")
    if not splits:
        print("No valid splits generated. Check date range.")
        return 2

    candidates = build_candidates(args.max_candidates)
    print(f"Evaluating {len(candidates)} candidates across {len(splits)} splits...")

    results: list[dict[str, Any]] = []
    started = time.time()
    for idx, candidate in enumerate(candidates, start=1):
        print(f"[{idx}/{len(candidates)}] candidate={candidate}")
        res = evaluate_candidate(
            base_cfg=base_cfg,
            candidate=candidate,
            splits=splits,
            dd_cap=args.dd_cap,
            oos_daily_target_pct=args.oos_daily_target_pct,
            python_bin=args.python_bin,
            limit_symbols=limit_symbols,
            stream_output=args.stream_backtest_output,
        )
        results.append(res)

    out_dir = write_outputs(results, splits)
    passed = sum(1 for r in results if r.get("pass"))
    elapsed = time.time() - started
    print(f"Done in {elapsed:.1f}s. pass={passed}/{len(results)}")
    print(f"summary={out_dir / 'summary.json'}")
    print(f"leaderboard={out_dir / 'leaderboard.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
