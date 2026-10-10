"""Employees, punch-in/out, listing, corrections and concurrency against a live MongoDB."""
import threading
from datetime import datetime

import pytest
from pymongo import MongoClient

from tests.conftest import ist_ms, make_employee, make_log

pytestmark = pytest.mark.mongo

EMPLOYEE = {"emp_code": "EMP0101", "name": "Asha Rao", "email": "asha@example.com", "department": "Engineering", "joined_on": "2026-01-05"}
NIGHT_EMPLOYEE = EMPLOYEE | {"emp_code": "EMP0102", "email": "meera@example.com", "shift_start": "22:00", "shift_end": "06:00"}
RECORD_KEYS = {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day", "history"}


def run_in_parallel(n, request):
    barrier, results = threading.Barrier(n), []

    def worker(i):
        barrier.wait()
        results.append(request(i))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


@pytest.fixture
def employees(client, db):
    assert client.post("/employees", json=EMPLOYEE).status_code == 201
    assert client.post("/employees", json=NIGHT_EMPLOYEE).status_code == 201


def punch_in(client, ms, emp_code="EMP0101", **extra):
    return client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ms, **extra})


def punch_out(client, ms, emp_code="EMP0101"):
    return client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": ms})


# --------------------------------------------------------------------------- employees and health
def test_health(client, db):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_employee_returns_contract_shape(client, db):
    before = int(datetime.now().timestamp() * 1000)
    r = client.post("/employees", json=EMPLOYEE)
    assert r.status_code == 201
    body = r.json()
    assert body.keys() == {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}
    assert body["shift_start"] == "09:30" and body["shift_end"] == "18:30"
    assert isinstance(body["created_at"], int) and abs(body["created_at"] - before) < 60_000


def test_duplicate_employee_is_409(client, employees):
    assert client.post("/employees", json=EMPLOYEE).status_code == 409


@pytest.mark.parametrize(
    "override",
    [
        {"emp_code": "E1"},
        {"emp_code": "EMP12"},
        {"email": "not-an-email"},
        {"name": ""},
        {"department": "x" * 51},
        {"shift_start": "25:00"},
        {"shift_start": "9:30"},
        {"shift_start": "10:00", "shift_end": "10:00"},
        {"joined_on": "2026-02-30"},
        {"joined_on": "05-01-2026"},
    ],
)
def test_invalid_employee_is_422(client, db, override):
    assert client.post("/employees", json=EMPLOYEE | override).status_code == 422


def test_concurrent_duplicate_creates_yield_one_201(client, db):
    codes = run_in_parallel(8, lambda i: client.post("/employees", json=EMPLOYEE).status_code)
    assert sorted(codes) == [201] + [409] * 7


def test_list_employees_sorted_filtered_and_paginated(client, db):
    db.employees.insert_many([make_employee(f"EMP{n:04d}", department="Sales" if n % 2 else "Eng") for n in (5, 3, 1, 4, 2)])
    first = client.get("/employees", params={"page_size": 2}).json()
    assert [e["emp_code"] for e in first["items"]] == ["EMP0001", "EMP0002"] and first["total"] == 5
    second = client.get("/employees", params={"page": 2, "page_size": 2}).json()
    assert [e["emp_code"] for e in second["items"]] == ["EMP0003", "EMP0004"]
    sales = client.get("/employees", params={"department": "Sales"}).json()
    assert sales["total"] == 3 and [e["emp_code"] for e in sales["items"]] == ["EMP0001", "EMP0003", "EMP0005"]
    assert client.get("/employees", params={"department": "sales"}).json()["total"] == 0  # case-sensitive
    assert client.get("/employees", params={"page": 0}).status_code == 422
    assert client.get("/employees", params={"page_size": 101}).status_code == 422


# --------------------------------------------------------------------------- punch-in
def test_punch_in_creates_record(client, employees):
    r = punch_in(client, ist_ms(2026, 7, 6, 9, 28))
    assert r.status_code == 201
    assert r.json() == {
        "emp_code": "EMP0101",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": ist_ms(2026, 7, 6, 9, 28),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }


def test_punch_in_stores_bson_date_and_truncates(client, employees, db):
    r = punch_in(client, ist_ms(2026, 7, 6, 9, 40, 0, 900), status="WFH")
    assert r.json()["punch_in"] == ist_ms(2026, 7, 6, 9, 40, 0) and r.json()["late_minutes"] == 0
    stored = db.attendance_logs.find_one({"emp_code": "EMP0101"})
    assert isinstance(stored["punch_in"], datetime) and stored["status"] == "WFH"
    assert int(stored["punch_in"].timestamp() * 1000) == ist_ms(2026, 7, 6, 9, 40, 0)


def test_punch_in_late_by_one_second_past_grace(client, employees):
    assert punch_in(client, ist_ms(2026, 7, 6, 9, 40, 1)).json()["late_minutes"] == 10


def test_punch_in_date_is_ist_calendar_date(client, employees):
    assert punch_in(client, ist_ms(2026, 7, 6, 0, 10)).json()["date"] == "2026-07-06"


def test_punch_in_overnight_after_midnight_belongs_to_previous_day(client, employees):
    r = punch_in(client, ist_ms(2026, 7, 7, 0, 30), emp_code="EMP0102")
    assert r.status_code == 201
    assert r.json()["date"] == "2026-07-06" and r.json()["late_minutes"] == 150


def test_duplicate_punch_in_same_attendance_date_is_409(client, employees):
    assert punch_in(client, ist_ms(2026, 7, 6, 9, 28)).status_code == 201
    assert punch_in(client, ist_ms(2026, 7, 6, 14, 0)).status_code == 409


def test_punch_in_without_timestamp_uses_now(client, employees):
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0101"})
    assert r.status_code == 201
    assert abs(r.json()["punch_in"] - datetime.now().timestamp() * 1000) < 60_000
    assert r.json()["punch_in"] % 1000 == 0


def test_punch_in_unknown_employee_is_404(client, employees):
    assert punch_in(client, ist_ms(2026, 7, 6, 9, 28), emp_code="EMP9999").status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"emp_code": "EMP0101", "punched_at": 1783312500},  # seconds, not milliseconds
        {"emp_code": "EMP0101", "punched_at": 1783312500000.0},
        {"emp_code": "EMP0101", "punched_at": "1783312500000"},
        {"emp_code": "EMP0101", "punched_at": True},
        {"emp_code": "EMP0101", "punched_at": 4102444800001},
        {"emp_code": "EMP0101", "punched_at": None},
        {"emp_code": "EMP0101", "status": "ABSENT"},
        {"emp_code": "EMP0101", "status": "LEAVE"},
        {"emp_code": "EMP0101", "status": "present"},
        {"punched_at": 1783312500000},
    ],
)
def test_invalid_punch_in_is_422(client, employees, body):
    assert client.post("/attendance/punch-in", json=body).status_code == 422


def test_concurrent_identical_punch_ins_yield_one_201(client, employees, db):
    codes = run_in_parallel(10, lambda i: punch_in(client, ist_ms(2026, 7, 6, 9, 28)).status_code)
    assert sorted(codes) == [201] + [409] * 9
    assert db.attendance_logs.count_documents({"emp_code": "EMP0101"}) == 1


# --------------------------------------------------------------------------- punch-out
def test_punch_out_computes_derived_fields(client, employees):
    punch_in(client, ist_ms(2026, 7, 6, 10, 5))
    r = punch_out(client, ist_ms(2026, 7, 6, 19, 10))
    assert r.status_code == 200
    body = r.json()
    assert body["punch_out"] == ist_ms(2026, 7, 6, 19, 10)
    assert (body["work_hours"], body["late_minutes"], body["overtime_minutes"], body["half_day"]) == (9.08, 35, 40, False)
    assert body["history"] == []


def test_punch_out_work_hours_half_up(client, employees):
    punch_in(client, ist_ms(2026, 7, 6, 9, 0))
    assert punch_out(client, ist_ms(2026, 7, 6, 17, 2, 42)).json()["work_hours"] == 8.05


def test_punch_out_half_day(client, employees):
    punch_in(client, ist_ms(2026, 7, 7, 9, 30), status="WFH")
    body = punch_out(client, ist_ms(2026, 7, 7, 13, 0)).json()
    assert body["work_hours"] == 3.5 and body["half_day"] is True


def test_punch_out_across_midnight_for_overnight_shift(client, employees):
    punch_in(client, ist_ms(2026, 7, 6, 21, 55), emp_code="EMP0102")
    body = punch_out(client, ist_ms(2026, 7, 7, 6, 40), emp_code="EMP0102").json()
    assert body["date"] == "2026-07-06"
    assert (body["work_hours"], body["overtime_minutes"], body["late_minutes"]) == (8.75, 40, 0)


def test_punch_out_closes_most_recent_record(client, employees, db):
    punch_in(client, ist_ms(2026, 7, 6, 9, 30))
    punch_in(client, ist_ms(2026, 7, 7, 9, 30))
    assert punch_out(client, ist_ms(2026, 7, 7, 18, 30)).json()["date"] == "2026-07-07"
    assert db.attendance_logs.find_one({"date": "2026-07-06"})["punch_out"] is None


def test_punch_out_without_punch_in_is_404(client, employees):
    assert punch_out(client, ist_ms(2026, 7, 6, 18, 30)).status_code == 404
    punch_in(client, ist_ms(2026, 7, 6, 9, 30))
    assert punch_out(client, ist_ms(2026, 7, 6, 9, 0)).status_code == 404  # before the only punch-in


def test_punch_out_unknown_employee_is_404(client, employees):
    assert punch_out(client, ist_ms(2026, 7, 6, 18, 30), emp_code="EMP9999").status_code == 404


def test_second_punch_out_is_409(client, employees):
    punch_in(client, ist_ms(2026, 7, 6, 9, 30))
    assert punch_out(client, ist_ms(2026, 7, 6, 18, 30)).status_code == 200
    assert punch_out(client, ist_ms(2026, 7, 6, 18, 45)).status_code == 409


def test_punch_out_time_bounds(client, employees):
    punch_in(client, ist_ms(2026, 7, 6, 9, 30))
    assert punch_out(client, ist_ms(2026, 7, 6, 9, 30)).status_code == 422  # equal to punch-in
    r = punch_out(client, ist_ms(2026, 7, 7, 9, 30, 1))  # 24h and 1s later
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)
    assert punch_out(client, ist_ms(2026, 7, 7, 9, 30)).status_code == 200  # exactly 24h is allowed


def test_concurrent_punch_outs_yield_one_200(client, employees):
    punch_in(client, ist_ms(2026, 7, 6, 9, 30))
    codes = run_in_parallel(10, lambda i: punch_out(client, ist_ms(2026, 7, 6, 18, 30) + i * 1000).status_code)
    assert sorted(codes) == [200] + [409] * 9


# --------------------------------------------------------------------------- listing
def test_list_attendance_order_filters_and_shape(client, db):
    db.attendance_logs.insert_many(
        [
            make_log("EMP0002", "2026-07-06"),
            make_log("EMP0001", "2026-07-06"),
            make_log("EMP0001", "2026-07-07", status="LEAVE"),
            make_log("EMP0003", "2026-07-08", work_hours=8.0, legacy=True),
        ]
    )
    body = client.get("/attendance").json()
    assert [(i["date"], i["emp_code"]) for i in body["items"]] == [
        ("2026-07-08", "EMP0003"),
        ("2026-07-07", "EMP0001"),
        ("2026-07-06", "EMP0001"),
        ("2026-07-06", "EMP0002"),
    ]
    legacy = body["items"][0]
    assert legacy.keys() == RECORD_KEYS and legacy["half_day"] is False and legacy["history"] == []
    assert isinstance(legacy["punch_in"], int)

    page = client.get("/attendance", params={"date_from": "2026-07-06", "date_to": "2026-07-07", "page": 2, "page_size": 2}).json()
    assert page["total"] == 3 and [(i["date"], i["emp_code"]) for i in page["items"]] == [("2026-07-06", "EMP0002")]
    assert client.get("/attendance", params={"status": "LEAVE"}).json()["total"] == 1
    assert client.get("/attendance", params={"emp_code": "EMP0001"}).json()["total"] == 2


@pytest.mark.parametrize(
    "params",
    [{"date_from": "2026-07-08", "date_to": "2026-07-01"}, {"date_from": "2026-13-01"}, {"date_to": "07/01/2026"}, {"status": "HOLIDAY"}, {"page_size": 0}],
)
def test_list_attendance_invalid_params_are_422(client, db, params):
    assert client.get("/attendance", params=params).status_code == 422


# --------------------------------------------------------------------------- regularization (PATCH)
@pytest.fixture
def closed_late_record(client, employees):
    punch_in(client, ist_ms(2026, 7, 7, 10, 5))
    punch_out(client, ist_ms(2026, 7, 7, 18, 30))


def patch(client, body, date="2026-07-07", emp_code="EMP0101"):
    return client.patch(f"/attendance/{emp_code}/{date}", json={"reason": "biometric glitch", "regularized_by": "hr.admin"} | body)


def test_regularize_punch_in_recomputes_and_appends_history(client, closed_late_record, db):
    r = patch(client, {"punch_in": ist_ms(2026, 7, 7, 9, 28)})
    assert r.status_code == 200
    body = r.json()
    assert (body["late_minutes"], body["work_hours"], body["punch_in"]) == (0, 9.03, ist_ms(2026, 7, 7, 9, 28))
    [entry] = body["history"]
    assert entry["by"] == "hr.admin" and entry["reason"] == "biometric glitch" and isinstance(entry["at"], int)
    assert entry["changes"] == {
        "punch_in": {"from": ist_ms(2026, 7, 7, 10, 5), "to": ist_ms(2026, 7, 7, 9, 28)},
        "late_minutes": {"from": 35, "to": 0},
        "work_hours": {"from": 8.42, "to": 9.03},
    }
    stored = db.attendance_logs.find_one({"emp_code": "EMP0101", "date": "2026-07-07"})
    assert isinstance(stored["history"][0]["changes"]["punch_in"]["from"], datetime)  # BSON dates inside history


def test_second_correction_appends_without_editing_the_first(client, closed_late_record):
    first = patch(client, {"punch_in": ist_ms(2026, 7, 7, 9, 28)}).json()["history"][0]
    r = patch(client, {"punch_out": ist_ms(2026, 7, 7, 19, 5)}, )
    history = r.json()["history"]
    assert len(history) == 2 and history[0] == first
    assert history[1]["changes"] == {
        "punch_out": {"from": ist_ms(2026, 7, 7, 18, 30), "to": ist_ms(2026, 7, 7, 19, 5)},
        "work_hours": {"from": 9.03, "to": 9.62},
        "overtime_minutes": {"from": 0, "to": 35},
    }


def test_regularize_to_leave_clears_punches(client, closed_late_record):
    body = patch(client, {"status": "LEAVE"}).json()
    assert (body["punch_in"], body["punch_out"], body["work_hours"], body["late_minutes"], body["half_day"]) == (None, None, None, 0, False)
    assert set(body["history"][0]["changes"]) == {"status", "punch_in", "punch_out", "work_hours", "late_minutes"}


def test_regularize_absent_record_back_to_present(client, employees, db):
    db.attendance_logs.insert_one(make_log("EMP0101", "2026-07-08", status="ABSENT"))
    assert patch(client, {"status": "PRESENT"}, date="2026-07-08").status_code == 422  # no punch_in anywhere
    r = patch(client, {"status": "PRESENT", "punch_in": ist_ms(2026, 7, 8, 9, 50)}, date="2026-07-08")
    assert r.status_code == 200 and r.json()["late_minutes"] == 20 and r.json()["work_hours"] is None


def test_regularize_legacy_record_without_history_field(client, employees, db):
    db.attendance_logs.insert_one(make_log("EMP0101", "2026-07-09", work_hours=9.0, legacy=True))
    r = patch(client, {"status": "WFH"}, date="2026-07-09")
    assert r.status_code == 200 and len(r.json()["history"]) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"status": "LEAVE", "punch_in": ist_ms(2026, 7, 7, 9, 28)},
        {"status": "ABSENT", "punch_out": ist_ms(2026, 7, 7, 18, 0)},
        {"punch_in": ist_ms(2026, 7, 8, 9, 28)},  # moves the record to another attendance date
        {"punch_out": ist_ms(2026, 7, 7, 9, 0)},  # before punch_in
        {"punch_out": ist_ms(2026, 7, 8, 10, 6)},  # more than 24h after punch_in
        {"punch_in": ist_ms(2026, 7, 7, 10, 5)},  # same as stored: changes nothing
        {"status": "PRESENT"},  # same as stored: changes nothing
        {"punch_in": None},
        {"punch_in": 1783312500},
        {"status": "SICK"},
        {"reason": "abc"},
        {"regularized_by": ""},
    ],
)
def test_invalid_regularization_is_422_and_leaves_record_untouched(client, closed_late_record, db, body):
    before = db.attendance_logs.find_one({"emp_code": "EMP0101"})
    r = patch(client, body)
    assert r.status_code == 422
    assert db.attendance_logs.find_one({"emp_code": "EMP0101"}) == before


def test_regularize_missing_required_fields_is_422(client, closed_late_record):
    assert client.patch("/attendance/EMP0101/2026-07-07", json={"status": "LEAVE"}).status_code == 422


def test_regularize_ignores_client_sent_derived_fields(client, closed_late_record):
    body = patch(client, {"punch_in": ist_ms(2026, 7, 7, 9, 28), "late_minutes": 999, "work_hours": 1.0}).json()
    assert body["late_minutes"] == 0 and body["work_hours"] == 9.03


def test_regularize_not_found(client, closed_late_record):
    assert patch(client, {"status": "LEAVE"}, emp_code="EMP9999").status_code == 404
    assert patch(client, {"status": "LEAVE"}, date="2026-07-01").status_code == 404
    assert patch(client, {"status": "LEAVE"}, date="2026-7-1").status_code == 422


@pytest.mark.parametrize("make_body", [lambda i: {"punch_out": ist_ms(2026, 7, 7, 18, 31 + i)}, lambda i: {"status": ("WFH", "ON_DUTY")[i % 2]}])
def test_concurrent_regularizations_never_lose_history(client, closed_late_record, db, make_body):
    state = db.attendance_logs.find_one({"emp_code": "EMP0101"})
    for _ in range(5):
        results = run_in_parallel(8, lambda i: patch(client, make_body(i)))
        assert {r.status_code for r in results} <= {200, 409, 422}  # 422: a later request that no longer changes anything
    history = db.attendance_logs.find_one({"emp_code": "EMP0101"})["history"]
    assert history
    # Replaying the history from the original record must reproduce every "from" value: no entry was based on a
    # state that another concurrent correction had already replaced.
    for entry in history:
        for field, change in entry["changes"].items():
            assert change["from"] == state[field]
            state[field] = change["to"]


# --------------------------------------------------------------------------- database failures
def test_database_down_gives_503_not_500(client, db):
    real_db = client.app.state.db
    dead = MongoClient("mongodb://127.0.0.1:1", serverSelectionTimeoutMS=200)
    client.app.state.db = dead["unreachable"]
    try:
        assert client.get("/health").status_code == 503
        assert client.get("/employees").status_code == 503
        assert punch_in(client, ist_ms(2026, 7, 6, 9, 30)).status_code == 503
    finally:
        client.app.state.db = real_db
        dead.close()


@pytest.mark.parametrize("path", ["/attendance", "/employees", "/admin/explain/attendance_list"])
def test_page_beyond_64_bit_offset_is_422_not_500(client, db, path):
    assert client.get(path, params={"page": 10**20}).status_code == 422
    assert client.get(path, params={"page": 2**63 // 100, "page_size": 100}).status_code == 200  # largest allowed page
