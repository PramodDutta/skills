import os

# Unit tests must never call a real LLM: force every lesson into OFFLINE mode.
# (Live tests in test_live_llm.py switch this off explicitly.)
os.environ["QA_OFFLINE"] = "1"

import pytest  # noqa: E402

from booking_api import start_booking_api  # noqa: E402


@pytest.fixture
def booking_api():
    """A fresh Booking API per test - tests never share data."""
    with start_booking_api() as api:
        yield api


@pytest.fixture
def flaky_booking_api():
    """The first two GET /booking/{id} calls return 503."""
    with start_booking_api(flaky_503=2) as api:
        yield api


@pytest.fixture
def no_sleep(monkeypatch):
    """Skip the lessons' artificial delays (backoff, simulated test time)."""
    import time
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
