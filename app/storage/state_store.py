"""State persistence for restart recovery."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from loguru import logger

from app.core.events import Side
from app.trading.signals import OpenPosition


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal types."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, Side):
            return obj.value
        return super().default(obj)


class StateStore:
    """
    State persistence for restart recovery.

    Saves:
    - Open positions with entry details
    - Pending orders
    - Strategy state (optional)

    Used for reconciliation after restart.
    """

    def __init__(self, state_file: str | Path = "state.json") -> None:
        """
        Initialize state store.

        Args:
            state_file: Path to state file
        """
        self._state_file = Path(state_file)
        self._state: dict[str, Any] = {}

    def load(self) -> dict[str, Any]:
        """
        Load state from file.

        Returns:
            State dictionary
        """
        if not self._state_file.exists():
            logger.info("No state file found, starting fresh")
            return {}

        try:
            with open(self._state_file) as f:
                self._state = json.load(f)
            logger.info(f"Loaded state from {self._state_file}")
            return self._state
        except Exception as e:
            logger.error(f"Error loading state: {e}")
            return {}

    def save(self) -> None:
        """Save current state to file."""
        try:
            # Create backup
            if self._state_file.exists():
                backup_path = self._state_file.with_suffix(".json.bak")
                self._state_file.rename(backup_path)

            with open(self._state_file, "w") as f:
                json.dump(self._state, f, cls=DecimalEncoder, indent=2)

            logger.debug(f"Saved state to {self._state_file}")
        except Exception as e:
            logger.error(f"Error saving state: {e}")

    def save_position(self, position: OpenPosition) -> None:
        """
        Save an open position.

        Args:
            position: Position to save
        """
        if "positions" not in self._state:
            self._state["positions"] = {}

        self._state["positions"][position.symbol] = {
            "symbol": position.symbol,
            "side": position.side.value,
            "entry_time": position.entry_time.isoformat(),
            "entry_price": str(position.entry_price),
            "size": str(position.size),
            "sl_price": str(position.sl_price),
            "tp_price": str(position.tp_price),
            "entry_fees": str(position.entry_fees),
            "metadata": position.signal_metadata,
        }

        self.save()

    def remove_position(self, symbol: str) -> None:
        """
        Remove a position from state.

        Args:
            symbol: Symbol to remove
        """
        if "positions" in self._state and symbol in self._state["positions"]:
            del self._state["positions"][symbol]
            self.save()

    def get_positions(self) -> list[OpenPosition]:
        """
        Get saved positions.

        Returns:
            List of OpenPosition objects
        """
        positions = []
        for data in self._state.get("positions", {}).values():
            try:
                position = OpenPosition(
                    symbol=data["symbol"],
                    side=Side(data["side"]),
                    entry_time=datetime.fromisoformat(data["entry_time"]),
                    entry_price=Decimal(data["entry_price"]),
                    size=Decimal(data["size"]),
                    sl_price=Decimal(data["sl_price"]),
                    tp_price=Decimal(data["tp_price"]),
                    entry_fees=Decimal(data["entry_fees"]),
                    signal_metadata=data.get("metadata", {}),
                )
                positions.append(position)
            except Exception as e:
                logger.error(f"Error loading position: {e}")

        return positions

    def save_pending_order(self, order_id: str, order_data: dict[str, Any]) -> None:
        """Save a pending order."""
        if "pending_orders" not in self._state:
            self._state["pending_orders"] = {}

        self._state["pending_orders"][order_id] = order_data
        self.save()

    def remove_pending_order(self, order_id: str) -> None:
        """Remove a pending order."""
        if "pending_orders" in self._state and order_id in self._state["pending_orders"]:
            del self._state["pending_orders"][order_id]
            self.save()

    def get_pending_orders(self) -> dict[str, dict[str, Any]]:
        """Get saved pending orders."""
        return self._state.get("pending_orders", {}).copy()

    def save_strategy_state(self, state: dict[str, Any]) -> None:
        """Save strategy-specific state."""
        self._state["strategy"] = state
        self.save()

    def get_strategy_state(self) -> dict[str, Any]:
        """Get strategy-specific state."""
        return self._state.get("strategy", {}).copy()

    def set(self, key: str, value: Any) -> None:
        """Set a state value."""
        self._state[key] = value
        self.save()

    def get(self, key: str, default: Any = None) -> Any:
        """Get a state value."""
        return self._state.get(key, default)

    def clear(self) -> None:
        """Clear all state."""
        self._state = {}
        if self._state_file.exists():
            self._state_file.unlink()
