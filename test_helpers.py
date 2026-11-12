from datetime import datetime
from zoneinfo import ZoneInfo

from main import slot_is_outage, slot_start


def test_status_values():
    assert slot_is_outage("NO")
    assert slot_is_outage("FIRST")
    assert not slot_is_outage("YES")
    assert not slot_is_outage("MAYBE")


def test_slot_time_key():
    now = datetime(2026, 11, 12, 10, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
    assert slot_start("11:30-12:00", now).hour == 11
    assert slot_start("11:30-12:00", now).minute == 30


def test_slot_index():
    now = datetime(2026, 11, 12, 10, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
    assert slot_start("23", now).hour == 11
    assert slot_start("23", now).minute == 30
