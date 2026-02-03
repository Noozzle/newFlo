"""FastAPI web server for trading calendar UI."""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


def utc_today() -> date:
    """Get current date in UTC."""
    return datetime.now(timezone.utc).date()

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Config
from app.ui.bybit_client import BybitPnLClient
from app.ui.models import DayStats, MonthData

# Setup paths
UI_DIR = Path(__file__).parent
TEMPLATES_DIR = UI_DIR / "templates"
STATIC_DIR = UI_DIR / "static"

# Create FastAPI app
app = FastAPI(title="FloTrader Calendar", version="1.0.0")

# Mount static files
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Setup templates
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Global client (initialized on startup)
pnl_client: BybitPnLClient | None = None
_config: Config | None = None


@app.on_event("startup")
async def startup() -> None:
    """Initialize Bybit client on startup."""
    global pnl_client, _config
    _config = Config.from_yaml("config.yaml")
    pnl_client = BybitPnLClient(_config.bybit)


def get_calendar_data(year: int, month: int) -> tuple[MonthData, list[dict[str, Any]]]:
    """Fetch and aggregate PnL data for a month."""
    assert pnl_client is not None

    # Calculate date range
    start_date = datetime(year, month, 1)
    if month == 12:
        end_date = datetime(year + 1, 1, 1)
    else:
        end_date = datetime(year, month + 1, 1)

    # Fetch records from Bybit
    records = pnl_client.get_closed_pnl(start_date, end_date)

    # Aggregate by day
    day_data: dict[int, dict[str, Any]] = defaultdict(
        lambda: {"pnl": Decimal(0), "count": 0, "wins": 0, "losses": 0}
    )

    for record in records:
        day = record.closed_time.day
        day_data[day]["pnl"] += record.closed_pnl
        day_data[day]["count"] += 1
        if record.closed_pnl > 0:
            day_data[day]["wins"] += 1
        else:
            day_data[day]["losses"] += 1

    # Build day stats
    days = {
        d: DayStats(
            date=date(year, month, d),
            trade_count=data["count"],
            total_pnl=data["pnl"],
            winning_trades=data["wins"],
            losing_trades=data["losses"],
        )
        for d, data in day_data.items()
    }

    month_data = MonthData(
        year=year,
        month=month,
        days=days,
        month_pnl=sum(d.total_pnl for d in days.values()),
        month_trades=sum(d.trade_count for d in days.values()),
    )

    # Build calendar grid
    cal = calendar.Calendar(firstweekday=0)  # Monday first
    calendar_days = []

    for week in cal.monthdayscalendar(year, month):
        for day_num in week:
            if day_num == 0:
                calendar_days.append({
                    "number": "",
                    "css_class": "empty",
                    "stats": None,
                    "date_str": "",
                })
            else:
                stats = days.get(day_num)
                css_classes = ["day"]

                if stats:
                    if stats.total_pnl > 0:
                        css_classes.append("profitable")
                    elif stats.total_pnl < 0:
                        css_classes.append("losing")

                today = utc_today()
                if year == today.year and month == today.month and day_num == today.day:
                    css_classes.append("today")

                calendar_days.append({
                    "number": day_num,
                    "css_class": " ".join(css_classes),
                    "stats": stats,
                    "date_str": f"{year}/{month}/{day_num}",
                })

    return month_data, calendar_days


@app.get("/", response_class=HTMLResponse)
async def calendar_view(
    request: Request,
    year: int | None = None,
    month: int | None = None,
) -> HTMLResponse:
    """Render calendar view for specified month."""
    today = utc_today()
    year = year or today.year
    month = month or today.month

    # Validate month
    if month < 1 or month > 12:
        month = today.month

    # Calculate prev/next month
    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1

    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1

    # Get data
    month_data, calendar_days = get_calendar_data(year, month)

    return templates.TemplateResponse(
        "calendar.html",
        {
            "request": request,
            "month_data": month_data,
            "calendar_days": calendar_days,
            "today": today,
            "prev_year": prev_year,
            "prev_month": prev_month,
            "next_year": next_year,
            "next_month": next_month,
            "testnet": _config.bybit.testnet if _config else True,
        },
    )


@app.get("/day/{year}/{month}/{day}", response_class=HTMLResponse)
async def day_detail(
    request: Request,
    year: int,
    month: int,
    day: int,
) -> HTMLResponse:
    """Render detailed view for a specific day."""
    assert pnl_client is not None

    target_date = date(year, month, day)
    trades = pnl_client.get_day_trades(target_date)

    # Sort by time
    trades.sort(key=lambda t: t.closed_time)

    # Calculate stats
    total_pnl = sum(t.closed_pnl for t in trades)
    winning = [t for t in trades if t.closed_pnl > 0]
    losing = [t for t in trades if t.closed_pnl <= 0]

    return templates.TemplateResponse(
        "day.html",
        {
            "request": request,
            "date": target_date,
            "trades": trades,
            "total_pnl": total_pnl,
            "trade_count": len(trades),
            "winning_count": len(winning),
            "losing_count": len(losing),
            "win_rate": len(winning) / len(trades) * 100 if trades else 0,
            "testnet": _config.bybit.testnet if _config else True,
        },
    )


@app.get("/api/debug/{year}/{month}")
async def debug_month_data(year: int, month: int):
    """Debug endpoint to see raw data from Bybit API."""
    assert pnl_client is not None

    start_date = datetime(year, month, 1)
    if month == 12:
        end_date = datetime(year + 1, 1, 1)
    else:
        end_date = datetime(year, month + 1, 1)

    records = pnl_client.get_closed_pnl(start_date, end_date)

    return {
        "query": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "testnet": _config.bybit.testnet if _config else None,
            "category": _config.bybit.category if _config else None,
        },
        "total_records": len(records),
        "records": [
            {
                "symbol": r.symbol,
                "side": r.side,
                "closed_pnl": str(r.closed_pnl),
                "closed_time": r.closed_time.isoformat(),
                "entry_price": str(r.avg_entry_price),
                "exit_price": str(r.avg_exit_price),
                "size": str(r.closed_size),
            }
            for r in records
        ],
    }


@app.get("/api/debug/raw/{year}/{month}")
async def debug_raw_api(year: int, month: int):
    """Debug endpoint to see raw Bybit API response."""
    assert pnl_client is not None
    assert _config is not None

    start_date = datetime(year, month, 1)
    if month == 12:
        end_date = datetime(year + 1, 1, 1)
    else:
        end_date = datetime(year, month + 1, 1)

    # Make direct API call
    params = {
        "category": _config.bybit.category,
        "startTime": int(start_date.timestamp() * 1000),
        "endTime": int(end_date.timestamp() * 1000),
        "limit": 100,
    }

    try:
        result = pnl_client._client.get_closed_pnl(**params)
        return {
            "request_params": params,
            "response": result,
        }
    except Exception as e:
        return {"error": str(e)}


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the UI server."""
    import uvicorn

    uvicorn.run(app, host=host, port=port)
