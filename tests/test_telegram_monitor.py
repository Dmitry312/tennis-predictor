from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tennis_predictor.ingestion.telegram_monitor import (
    render_alert, render_daily, report_date, send_message,
)


def test_moscow_day_rollover():
    utc = datetime(2026, 11, 15, 21, 30, tzinfo=ZoneInfo("UTC"))
    assert report_date(utc).isoformat() == "2026-11-15"


def test_unknown_metrics_are_not_zero():
    msg = render_daily(report_date(datetime(2026, 11, 15, tzinfo=ZoneInfo("Europe/Moscow"))), {})
    assert "нет данных" in msg
    assert "Коэффициенты" not in msg


def test_alert_sanitizes_line_breaks():
    assert "job / error\ntext" in render_alert("job", "error", "text\nother")


def test_send_requires_credentials():
    with pytest.raises(ValueError):
        send_message("hi", "", "")


def test_send_rejects_oversize_before_network():
    with pytest.raises(ValueError):
        send_message("x" * 4001, "token", "123")
