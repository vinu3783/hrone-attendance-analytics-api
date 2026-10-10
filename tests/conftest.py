"""Test fixtures.

Tests marked `mongo` need a live MongoDB 6.0+ at TEST_MONGO_URI (default mongodb://localhost:27017). They run against a
throwaway database with a random name that is dropped afterwards, so they never touch the database in MONGO_DB.
If MongoDB is unreachable those tests are skipped and the pure rule tests still run.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pymongo import MongoClient
from pymongo.errors import PyMongoError

IST = timezone(timedelta(hours=5, minutes=30))
TEST_MONGO_URI = os.getenv("TEST_MONGO_URI", "mongodb://localhost:27017")
TEST_DB = f"hrone_test_{uuid.uuid4().hex[:8]}"


def ist_ms(y, mo, d, h=0, mi=0, s=0, ms=0) -> int:
    """Epoch milliseconds for an IST wall-clock time."""
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000 + ms


def ist(y, mo, d, h=0, mi=0, s=0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(timezone.utc)


def _mongo_available() -> bool:
    try:
        MongoClient(TEST_MONGO_URI, serverSelectionTimeoutMS=1500).admin.command("ping")
        return True
    except PyMongoError:
        return False


MONGO_AVAILABLE = _mongo_available()


def pytest_collection_modifyitems(items):
    if MONGO_AVAILABLE:
        return
    skip = pytest.mark.skip(reason=f"MongoDB not reachable at {TEST_MONGO_URI}")
    for item in items:
        if "mongo" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    os.environ["MONGO_URI"] = TEST_MONGO_URI
    os.environ["MONGO_DB"] = TEST_DB
    from app.main import app

    with TestClient(app) as test_client:  # runs the lifespan: connects and creates indexes
        yield test_client
    MongoClient(TEST_MONGO_URI).drop_database(TEST_DB)


@pytest.fixture
def db(client):
    """Empty collections (indexes kept) for every test."""
    database = client.app.state.db
    database.employees.delete_many({})
    database.attendance_logs.delete_many({})
    return database


def make_employee(emp_code, department="Engineering", shift_start="09:30", shift_end="18:30", joined_on="2026-01-01", name=None):
    return {
        "emp_code": emp_code,
        "name": name or f"Name {emp_code}",
        "email": f"{emp_code.lower()}@example.com",
        "department": department,
        "shift_start": shift_start,
        "shift_end": shift_end,
        "joined_on": joined_on,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


def make_log(emp_code, day, status="PRESENT", work_hours=None, late=0, overtime=0, half_day=False, legacy=False):
    """A stored attendance record with explicit derived values (analytics read stored values, never recompute)."""
    present = status in ("PRESENT", "WFH", "ON_DUTY")
    y, m, d = map(int, day.split("-"))
    doc = {
        "emp_code": emp_code,
        "date": day,
        "status": status,
        "punch_in": ist(y, m, d, 9, 30) if present else None,
        "punch_out": ist(y, m, d, 18, 30) if present and work_hours is not None else None,
        "work_hours": work_hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": half_day,
        "history": [],
    }
    if legacy:
        del doc["half_day"], doc["history"]
    return doc
