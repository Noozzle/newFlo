"""FastAPI web server for trading calendar UI."""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

# Try to get local timezone
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("localtime")
except Exception:
    LOCAL_TZ = None


def get_today(use_utc: bool = True) -> date:
    """Get current date in UTC or local time."""
    if use_utc:
        return datetime.now(timezone.utc).date()
    else:
        return date.today()


def convert_to_tz(dt: datetime, use_utc: bool = True) -> datetime:
    """Convert datetime to UTC or local time."""
    if use_utc:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    else:
        if dt.tzinfo is None:
            return dt
        return dt.astimezone() if LOCAL_TZ is None else dt.astimezone(LOCAL_TZ)

import asyncio
import json

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger

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


def get_calendar_data(year: int, month: int, use_utc: bool = True) -> tuple[MonthData, list[dict[str, Any]]]:
    """Fetch and aggregate PnL data for a month."""
    assert pnl_client is not None

    # Calculate date range (always fetch in UTC from API)
    start_date = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end_date = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end_date = datetime(year, month + 1, 1, tzinfo=timezone.utc)

    # Fetch records from Bybit
    records = pnl_client.get_closed_pnl(start_date, end_date)

    # Aggregate by ENTRY day (convert to selected timezone)
    day_data: dict[int, dict[str, Any]] = defaultdict(
        lambda: {"pnl": Decimal(0), "count": 0, "wins": 0, "losses": 0}
    )

    for record in records:
        # Convert ENTRY time to selected timezone for day grouping
        local_time = convert_to_tz(record.entry_time, use_utc)
        day = local_time.day
        # Only count if in the requested month (timezone conversion may shift days)
        if local_time.year == year and local_time.month == month:
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

                today = get_today(use_utc)
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
    tz: str = "utc",  # "utc" or "local"
) -> HTMLResponse:
    """Render calendar view for specified month."""
    use_utc = tz.lower() != "local"
    today = get_today(use_utc)
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
    month_data, calendar_days = get_calendar_data(year, month, use_utc)

    # Get balance and open positions
    balance = pnl_client.get_wallet_balance() if pnl_client else {"total_equity": Decimal("0")}
    total_equity = balance["total_equity"]

    open_positions = pnl_client.get_open_positions() if pnl_client else []
    total_unrealized = sum(p.unrealized_pnl for p in open_positions)
    total_unrealized_pct = (total_unrealized / total_equity * 100) if total_equity > 0 else Decimal("0")

    # Add % PnL to positions
    open_positions_with_pct = []
    for pos in open_positions:
        pnl_pct = (pos.unrealized_pnl / total_equity * 100) if total_equity > 0 else Decimal("0")
        open_positions_with_pct.append({
            "position": pos,
            "pnl_pct": pnl_pct,
        })

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
            "use_utc": use_utc,
            "tz": tz.lower(),
            "open_positions": open_positions_with_pct,
            "total_unrealized": total_unrealized,
            "total_unrealized_pct": total_unrealized_pct,
            "balance": total_equity,
        },
    )


@app.get("/day/{year}/{month}/{day}", response_class=HTMLResponse)
async def day_detail(
    request: Request,
    year: int,
    month: int,
    day: int,
    tz: str = "utc",
) -> HTMLResponse:
    """Render detailed view for a specific day."""
    assert pnl_client is not None

    use_utc = tz.lower() != "local"
    target_date = date(year, month, day)

    # Get current balance for % calculations
    balance = pnl_client.get_wallet_balance()
    total_equity = balance["total_equity"]

    # Fetch trades
    all_trades = pnl_client.get_day_trades(target_date)

    # Filter trades by ENTRY time in selected timezone and add % PnL
    trades_with_pct = []
    for trade in all_trades:
        local_entry_time = convert_to_tz(trade.entry_time, use_utc)
        if local_entry_time.date() == target_date:
            # Calculate PnL as % of current equity (approximation)
            pnl_pct = (trade.closed_pnl / total_equity * 100) if total_equity > 0 else Decimal("0")
            trades_with_pct.append({
                "trade": trade,
                "pnl_pct": pnl_pct,
            })

    # Sort by entry time
    trades_with_pct.sort(key=lambda t: t["trade"].entry_time)

    # Get ALL open positions (not just today's)
    all_open = pnl_client.get_open_positions()
    open_positions_with_pct = []
    for pos in all_open:
        pnl_pct = (pos.unrealized_pnl / total_equity * 100) if total_equity > 0 else Decimal("0")
        open_positions_with_pct.append({
            "position": pos,
            "pnl_pct": pnl_pct,
        })

    # Calculate stats (closed trades only)
    total_pnl = sum(t["trade"].closed_pnl for t in trades_with_pct)
    total_pnl_pct = (total_pnl / total_equity * 100) if total_equity > 0 else Decimal("0")
    winning = [t for t in trades_with_pct if t["trade"].closed_pnl > 0]
    losing = [t for t in trades_with_pct if t["trade"].closed_pnl <= 0]

    # Unrealized PnL from open positions
    unrealized_pnl = sum(p["position"].unrealized_pnl for p in open_positions_with_pct)
    unrealized_pnl_pct = (unrealized_pnl / total_equity * 100) if total_equity > 0 else Decimal("0")

    return templates.TemplateResponse(
        "day.html",
        {
            "request": request,
            "date": target_date,
            "trades": trades_with_pct,
            "open_positions": open_positions_with_pct,
            "total_pnl": total_pnl,
            "total_pnl_pct": total_pnl_pct,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "trade_count": len(trades_with_pct),
            "winning_count": len(winning),
            "losing_count": len(losing),
            "win_rate": len(winning) / len(trades_with_pct) * 100 if trades_with_pct else 0,
            "balance": total_equity,
            "testnet": _config.bybit.testnet if _config else True,
            "use_utc": use_utc,
            "tz": tz.lower(),
        },
    )


@app.get("/api/positions")
async def get_positions():
    """API endpoint to get current open positions with live prices."""
    assert pnl_client is not None

    positions = pnl_client.get_open_positions()
    return {
        "positions": [
            {
                "symbol": p.symbol,
                "side": p.side,
                "size": str(p.size),
                "entry_price": str(p.entry_price),
                "mark_price": str(p.mark_price),
                "unrealized_pnl": str(p.unrealized_pnl),
                "leverage": p.leverage,
                "created_time": p.created_time.isoformat(),
            }
            for p in positions
        ]
    }


@app.get("/api/debug/positions")
async def debug_positions():
    """Debug endpoint to see raw positions data from Bybit."""
    assert pnl_client is not None
    assert _config is not None

    try:
        result = pnl_client._client.get_positions(
            category=_config.bybit.category,
            settleCoin="USDT",
        )
        return {
            "config": {
                "category": _config.bybit.category,
                "testnet": _config.bybit.testnet,
            },
            "raw_response": result,
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/trade-chart/{symbol}")
async def get_trade_chart_data(
    symbol: str,
    entry_time: int,  # Unix timestamp in seconds
    exit_time: int,   # Unix timestamp in seconds
    interval: str = "1",
):
    """Get kline data for trade chart visualization."""
    assert pnl_client is not None

    # Interval in minutes
    interval_minutes = {"1": 1, "5": 5, "15": 15, "60": 60}.get(interval, 1)

    # Add padding: ~100 candles before entry, ~30 candles after exit
    padding_before = 100 * interval_minutes * 60  # 100 candles of history
    padding_after = 30 * interval_minutes * 60    # 30 candles after

    start_time = datetime.fromtimestamp(entry_time - padding_before, tz=timezone.utc)
    end_time = datetime.fromtimestamp(exit_time + padding_after, tz=timezone.utc)

    # Fetch klines
    klines = pnl_client.get_klines(
        symbol=symbol,
        interval=interval,
        start_time=start_time,
        end_time=end_time,
        limit=1000,
    )

    # Fetch TP/SL for this trade
    entry_dt = datetime.fromtimestamp(entry_time, tz=timezone.utc)
    exit_dt = datetime.fromtimestamp(exit_time, tz=timezone.utc)
    tpsl = pnl_client.get_trade_tpsl(symbol, entry_dt, exit_dt)

    return {
        "symbol": symbol,
        "interval": interval,
        "klines": klines,
        "take_profit": tpsl["take_profit"],
        "stop_loss": tpsl["stop_loss"],
    }


@app.get("/api/balance")
async def get_balance():
    """Get current wallet balance."""
    assert pnl_client is not None
    balance = pnl_client.get_wallet_balance()
    return {
        "total_equity": str(balance["total_equity"]),
        "available": str(balance["available"]),
        "usdt_balance": str(balance["usdt_balance"]),
    }


# WebSocket connections manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass


ws_manager = ConnectionManager()


@app.websocket("/ws/positions")
async def websocket_positions(websocket: WebSocket):
    """WebSocket endpoint for realtime position updates."""
    await ws_manager.connect(websocket)
    try:
        while True:
            # Fetch positions and balance every second
            if pnl_client:
                positions = pnl_client.get_open_positions()
                balance = pnl_client.get_wallet_balance()

                data = {
                    "type": "update",
                    "balance": {
                        "total_equity": str(balance["total_equity"]),
                        "available": str(balance["available"]),
                    },
                    "positions": [
                        {
                            "symbol": p.symbol,
                            "side": p.side,
                            "size": str(p.size),
                            "entry_price": str(p.entry_price),
                            "mark_price": str(p.mark_price),
                            "unrealized_pnl": str(p.unrealized_pnl),
                            "pnl_pct": str(
                                (p.unrealized_pnl / balance["total_equity"] * 100)
                                if balance["total_equity"] > 0 else Decimal("0")
                            ),
                            "leverage": p.leverage,
                            "liq_price": str(p.liq_price) if p.liq_price else None,
                            "position_value": str(p.position_value) if p.position_value else None,
                        }
                        for p in positions
                    ],
                }
                await websocket.send_json(data)

            await asyncio.sleep(1)  # Update every second
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        ws_manager.disconnect(websocket)


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
                "entry_time": r.entry_time.isoformat(),
                "exit_time": r.exit_time.isoformat(),
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
