"""Tests for CSV data loading and schema detection."""

from datetime import datetime
from decimal import Decimal
from io import StringIO

import pandas as pd
import pytest

from app.adapters.historical_data_feed import CSVSchemaDetector


def test_detect_kline_schema():
    """Test detection of OHLCV kline schema."""
    csv_data = """timestamp,open,high,low,close,volume
2024-01-01 12:00:00,50000,50100,49900,50050,100
"""
    df = pd.read_csv(StringIO(csv_data))
    schema = CSVSchemaDetector.detect_schema(df)

    assert schema["_type"] == "kline"
    assert schema["timestamp"] == "timestamp"
    assert schema["open"] == "open"
    assert schema["high"] == "high"
    assert schema["low"] == "low"
    assert schema["close"] == "close"
    assert schema["volume"] == "volume"


def test_detect_orderbook_schema():
    """Test detection of orderbook schema."""
    csv_data = """timestamp,bid_price,bid_size,ask_price,ask_size
2024-01-01 12:00:00,50000,10,50010,8
"""
    df = pd.read_csv(StringIO(csv_data))
    schema = CSVSchemaDetector.detect_schema(df)

    assert schema["_type"] == "orderbook"
    assert schema["bid_price"] == "bid_price"
    assert schema["bid_size"] == "bid_size"
    assert schema["ask_price"] == "ask_price"
    assert schema["ask_size"] == "ask_size"


def test_detect_trade_schema():
    """Test detection of trade schema."""
    csv_data = """timestamp,price,amount,side
2024-01-01 12:00:00,50000,0.1,buy
"""
    df = pd.read_csv(StringIO(csv_data))
    schema = CSVSchemaDetector.detect_schema(df)

    assert schema["_type"] == "trade"
    assert schema["price"] == "price"
    assert schema["amount"] == "amount"
    assert schema["side"] == "side"


def test_detect_timestamp_column_variations():
    """Test detection of various timestamp column names."""
    for ts_col in ["timestamp", "time", "datetime", "date", "ts"]:
        csv_data = f"""{ts_col},open,high,low,close,volume
2024-01-01,50000,50100,49900,50050,100
"""
        df = pd.read_csv(StringIO(csv_data))
        schema = CSVSchemaDetector.detect_schema(df)
        assert schema["timestamp"] == ts_col


def test_parse_timestamp_iso():
    """Test parsing ISO format timestamp."""
    ts = CSVSchemaDetector.parse_timestamp("2024-01-01 12:30:45.123456")
    assert ts == datetime(2024, 1, 1, 12, 30, 45, 123456)


def test_parse_timestamp_iso_t():
    """Test parsing ISO format with T separator."""
    ts = CSVSchemaDetector.parse_timestamp("2024-01-01T12:30:45.123456")
    assert ts == datetime(2024, 1, 1, 12, 30, 45, 123456)


def test_parse_timestamp_unix_seconds():
    """Test parsing Unix timestamp in seconds."""
    # 2024-01-01 00:00:00 UTC
    ts = CSVSchemaDetector.parse_timestamp(1704067200)
    assert ts.year == 2024
    assert ts.month == 1
    assert ts.day == 1


def test_parse_timestamp_unix_milliseconds():
    """Test parsing Unix timestamp in milliseconds."""
    # 2024-01-01 00:00:00 UTC in ms
    ts = CSVSchemaDetector.parse_timestamp(1704067200000)
    assert ts.year == 2024
    assert ts.month == 1
    assert ts.day == 1


def test_parse_timestamp_date_only():
    """Test parsing date-only format."""
    ts = CSVSchemaDetector.parse_timestamp("2024-01-15")
    assert ts == datetime(2024, 1, 15)


def test_case_insensitive_column_detection():
    """Test that column detection is case insensitive."""
    csv_data = """TIMESTAMP,OPEN,HIGH,LOW,CLOSE,VOLUME
2024-01-01,50000,50100,49900,50050,100
"""
    df = pd.read_csv(StringIO(csv_data))
    schema = CSVSchemaDetector.detect_schema(df)

    assert schema["_type"] == "kline"
    assert "timestamp" in schema


def test_alternative_column_names():
    """Test detection with alternative column names."""
    # Test alternative names for trades
    csv_data = """time,px,qty,direction
2024-01-01 12:00:00,50000,0.1,buy
"""
    df = pd.read_csv(StringIO(csv_data))
    schema = CSVSchemaDetector.detect_schema(df)

    assert "timestamp" in schema
    assert "price" in schema
    assert "amount" in schema
