"""Analytics endpoints against a hand-built dataset with the awkward cases the rules describe.

July 2026 has 23 weekdays (13 from Wed 15 July). Saturdays are 4/11/18/25, Sundays 5/12/19/26.
Every expected number below is worked out by hand in the comments; none is read back from the implementation.
"""
import pathlib
from datetime import datetime

import pytest
from bson import json_util

from app.main import round_half_up_expr
from tests.conftest import make_employee, make_log

pytestmark = pytest.mark.mongo

EMPLOYEES = [
    make_employee("EMP1001", "Eng", joined_on="2026-01-01"),
    make_employee("EMP1002", "Eng", joined_on="2026-07-15"),  # mid-month joiner
    make_employee("EMP1003", "Eng", joined_on="2026-01-01"),  # no logs at all
    make_employee("EMP1004", "Eng", joined_on="2026-08-03"),  # not yet joined in July
    make_employee("EMP2001", "Sales"),
    make_employee("EMP2002", "Sales"),
    make_employee("EMP2003", "Sales"),
    make_employee("EMP3001", "Night", shift_start="22:00", shift_end="06:00"),
    make_employee("EMP4001", "Future", joined_on="2026-09-01"),
]

LOGS = [
    make_log("EMP1001", "2026-06-30", work_hours=9.0, late=100),  # previous month
    make_log("EMP1001", "2026-07-01", work_hours=9.0, late=15),
    make_log("EMP1001", "2026-07-02", status="WFH", work_hours=4.0, half_day=True),
    make_log("EMP1001", "2026-07-04", work_hours=5.0, late=20, overtime=45),  # Saturday
    make_log("EMP1001", "2026-07-06", status="LEAVE"),
    make_log("EMP1001", "2026-07-07", status="ON_DUTY"),  # open record, no work_hours
    make_log("EMP1001", "2026-07-08", status="ABSENT"),
    make_log("EMP1001", "2026-07-09", work_hours=8.0, late=12, overtime=30, legacy=True),  # no half_day / history
    make_log("EMP1001", "2026-08-03", work_hours=8.0, late=50),  # next month
    make_log("EMP1002", "2026-07-15", work_hours=8.5),
    make_log("EMP1002", "2026-07-16", work_hours=3.0, late=47, half_day=True),
    make_log("EMP2001", "2026-07-01", work_hours=8.25, late=30),
    make_log("EMP2001", "2026-07-03", work_hours=8.0, late=17),
    make_log("EMP2002", "2026-07-01", work_hours=7.75, late=60),
    make_log("EMP2003", "2026-07-01", status="ON_DUTY", work_hours=8.98, late=10),
    make_log("EMP2003", "2026-07-02", status="LEAVE"),
    make_log("EMP3001", "2026-07-03", work_hours=8.0, overtime=40),
    make_log("EMP9999", "2026-07-01", work_hours=8.0, late=500),  # employee does not exist
]


@pytest.fixture
def dataset(db):
    db.employees.insert_many([dict(e) for e in EMPLOYEES])
    db.attendance_logs.insert_many([dict(log) for log in LOGS])


# --------------------------------------------------------------------------- employee monthly
def monthly(client, emp_code, month="2026-07"):
    return client.get(f"/analytics/employees/{emp_code}/monthly", params={"month": month})


def test_monthly_full_case(client, dataset):
    # present: 07-01 (1) + 07-02 half (0.5) + 07-07 ON_DUTY (1) + 07-09 legacy (1); the Saturday does not count.
    # late: 15 + 20 (Saturday still counts) + 12 = 47 over 3 records; overtime 45 + 30.
    assert monthly(client, "EMP1001").json() == {
        "emp_code": "EMP1001",
        "month": "2026-07",
        "working_days": 23,
        "present_days": 3.5,
        "leave_days": 1,
        "late_count": 3,
        "total_late_minutes": 47,
        "total_overtime_minutes": 75,
        "attendance_pct": 15.22,  # 3.5 / 23 * 100 = 15.217...
    }


def test_monthly_mid_month_joiner(client, dataset):
    body = monthly(client, "EMP1002").json()
    assert (body["working_days"], body["present_days"], body["attendance_pct"]) == (13, 1.5, 11.54)  # 1.5/13 = 11.538%


def test_monthly_employee_without_logs(client, dataset):
    body = monthly(client, "EMP1003").json()
    assert body["working_days"] == 23 and body["present_days"] == 0 and body["attendance_pct"] == 0.0
    assert (body["leave_days"], body["late_count"], body["total_late_minutes"], body["total_overtime_minutes"]) == (0, 0, 0, 0)


def test_monthly_before_joining_has_null_pct(client, dataset):
    body = monthly(client, "EMP1004").json()
    assert body["working_days"] == 0 and body["attendance_pct"] is None


def test_monthly_errors(client, dataset):
    assert monthly(client, "EMP9999").status_code == 404
    for bad in ("2026-13", "2026-7", "July"):
        assert monthly(client, "EMP1001", bad).status_code == 422
    assert client.get("/analytics/employees/EMP1001/monthly").status_code == 422


# --------------------------------------------------------------------------- department summary
def test_department_summary(client, dataset):
    body = client.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    assert body["month"] == "2026-07"
    assert body["items"] == [
        {
            # EMP1001, EMP1002 (joined 15th) and EMP1003 (no logs); EMP1004 joins in August.
            "department": "Eng",
            "headcount": 3,
            "present_days": 5.0,  # 3.5 + 1.5
            # Records: 9.0, 4.0, 5.0 (Saturday), 8.0, 8.5, 3.0 -> 37.5 / 6. A mean of per-employee means would be 6.13.
            "avg_work_hours": 6.25,
            "late_count": 4,
            "total_late_minutes": 94,
            "leave_count": 1,
            "on_duty_count": 1,
        },
        {"department": "Night", "headcount": 1, "present_days": 1.0, "avg_work_hours": 8.0, "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0},
        {
            "department": "Sales",
            "headcount": 3,
            "present_days": 4.0,
            # (8.25 + 8.0 + 7.75 + 8.98) / 4 = 8.245 exactly: half-up gives 8.25, $round and float round() give 8.24.
            "avg_work_hours": 8.25,
            "late_count": 4,
            "total_late_minutes": 117,
            "leave_count": 1,
            "on_duty_count": 1,
        },
    ]  # "Future" has headcount 0 in July and is omitted


def test_department_summary_previous_month_and_filter(client, dataset):
    june = client.get("/analytics/departments/summary", params={"month": "2026-06"}).json()["items"]
    eng = next(i for i in june if i["department"] == "Eng")
    assert (eng["headcount"], eng["present_days"], eng["avg_work_hours"], eng["total_late_minutes"]) == (2, 1.0, 9.0, 100)
    sales = next(i for i in june if i["department"] == "Sales")
    assert sales["headcount"] == 3 and sales["avg_work_hours"] is None and sales["present_days"] == 0

    only_sales = client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Sales"}).json()["items"]
    assert [i["department"] for i in only_sales] == ["Sales"]
    assert client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Nope"}).json()["items"] == []
    assert client.get("/analytics/departments/summary", params={"month": "2026-7"}).status_code == 422


def test_department_summary_empty_database(client, db):
    assert client.get("/analytics/departments/summary", params={"month": "2026-07"}).json() == {"month": "2026-07", "items": []}


# --------------------------------------------------------------------------- late leaderboard
def leaderboard(client, **params):
    return client.get("/analytics/leaderboard/late", params={"month": "2026-07", **params})


def rows(response):
    return [(i["rank"], i["emp_code"], i["total_late_minutes"], i["late_count"]) for i in response.json()["items"]]


def test_leaderboard_competition_ranking_with_ties(client, dataset):
    # EMP2002 60; EMP1001, EMP1002, EMP2001 tie on 47; EMP2003 10 -> ranks 1, 2, 2, 2, 5. EMP9999 has no employee.
    full = [(1, "EMP2002", 60, 1), (2, "EMP1001", 47, 3), (2, "EMP1002", 47, 1), (2, "EMP2001", 47, 2), (5, "EMP2003", 10, 1)]
    assert rows(leaderboard(client)) == full
    assert rows(leaderboard(client, limit=2)) == full[:4]  # the whole tie at rank 2 is kept
    assert rows(leaderboard(client, limit=4)) == full[:4]  # nobody has rank 3 or 4
    assert rows(leaderboard(client, limit=1)) == full[:1]
    first = leaderboard(client).json()["items"][0]
    assert first == {"rank": 1, "emp_code": "EMP2002", "name": "Name EMP2002", "department": "Sales", "total_late_minutes": 60, "late_count": 1}


def test_leaderboard_ranks_within_department(client, dataset):
    assert rows(leaderboard(client, department="Eng")) == [(1, "EMP1001", 47, 3), (1, "EMP1002", 47, 1)]
    assert rows(leaderboard(client, department="Sales", limit=2)) == [(1, "EMP2002", 60, 1), (2, "EMP2001", 47, 2)]
    assert rows(leaderboard(client, department="Night")) == []


def test_leaderboard_validation(client, dataset):
    assert leaderboard(client, limit=0).status_code == 422
    assert leaderboard(client, limit=51).status_code == 422
    assert client.get("/analytics/leaderboard/late").status_code == 422


# --------------------------------------------------------------------------- department trend
def trend(client, department, date_from, date_to):
    return client.get(f"/analytics/departments/{department}/trend", params={"from": date_from, "to": date_to})


def test_trend_gap_filling_weekends_and_moving_average(client, dataset):
    r = trend(client, "Eng", "2026-07-01", "2026-07-09")
    assert r.status_code == 200 and r.json()["department"] == "Eng"
    got = [(i["date"], i["is_working_day"], i["headcount"], i["present_count"], i["late_count"], i["attendance_rate"], i["moving_avg_7d"]) for i in r.json()["items"]]
    # Headcount 2 (EMP1001, EMP1003). Moving average = mean of the non-null rates in the last <= 7 rows of the range.
    assert got == [
        ("2026-07-01", True, 2, 1.0, 1, 0.5, 0.5),
        ("2026-07-02", True, 2, 0.5, 0, 0.25, 0.375),
        ("2026-07-03", True, 2, 0.0, 0, 0.0, 0.25),  # working day with no records: rate 0.0
        ("2026-07-04", False, 2, 1.0, 1, None, 0.25),  # Saturday: counted, but no rate
        ("2026-07-05", False, 2, 0.0, 0, None, 0.25),
        ("2026-07-06", True, 2, 0.0, 0, 0.0, 0.1875),  # LEAVE is not present
        ("2026-07-07", True, 2, 1.0, 0, 0.5, 0.25),  # (0.5 + 0.25 + 0 + 0 + 0.5) / 5
        ("2026-07-08", True, 2, 0.0, 0, 0.0, 0.15),  # window starts 07-02: (0.25 + 0 + 0 + 0.5 + 0) / 5
        ("2026-07-09", True, 2, 1.0, 1, 0.5, 0.2),  # window starts 07-03
    ]


def test_trend_headcount_grows_on_join_date(client, dataset):
    items = trend(client, "Eng", "2026-07-13", "2026-07-19").json()["items"]
    assert [i["headcount"] for i in items] == [2, 2, 3, 3, 3, 3, 3]
    assert [i["attendance_rate"] for i in items] == [0.0, 0.0, 0.3333, 0.1667, 0.0, None, None]
    assert [i["moving_avg_7d"] for i in items] == [0.0, 0.0, 0.1111, 0.125, 0.1, 0.1, 0.1]


def test_trend_department_with_nobody_joined_yet(client, dataset):
    items = trend(client, "Future", "2026-07-01", "2026-07-03").json()["items"]
    assert [(i["headcount"], i["attendance_rate"], i["moving_avg_7d"]) for i in items] == [(0, None, None)] * 3


def test_trend_one_row_per_day_for_a_full_quarter(client, dataset):
    items = trend(client, "Sales", "2026-07-01", "2026-09-30").json()["items"]  # 92 days, the maximum
    assert len(items) == 92 and items[0]["date"] == "2026-07-01" and items[-1]["date"] == "2026-09-30"
    assert items[0]["present_count"] == 3.0 and items[0]["late_count"] == 3


def test_trend_errors(client, dataset):
    assert trend(client, "Nope", "2026-07-01", "2026-07-09").status_code == 404
    assert trend(client, "Eng", "2026-07-09", "2026-07-01").status_code == 422
    assert trend(client, "Eng", "2026-07-01", "2026-10-01").status_code == 422  # 93 days
    assert trend(client, "Eng", "2026-07-01", "2026-02-30").status_code == 422
    assert client.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01"}).status_code == 422


# --------------------------------------------------------------------------- rounding in the database
def test_round_half_up_expression_in_mongodb(db):
    db.employees.insert_one({"x": 1})
    pipeline = [{"$project": {"_id": 0, "r": [{"$toDouble": round_half_up_expr(v, p)} for v, p in ((8.245, 2), (0.03125, 4), (2.675, 2), (0.125, 2), (1 / 3, 4))]}}]
    assert next(db.employees.aggregate(pipeline))["r"] == [8.25, 0.0313, 2.68, 0.13, 0.3333]


# --------------------------------------------------------------------------- the supplied sample data
@pytest.fixture
def sample_data(db):
    options = json_util.JSONOptions(tz_aware=True)
    for name in ("employees", "attendance_logs"):
        path = pathlib.Path(__file__).parent.parent / "sample_data" / f"{name}.json"
        db[name].insert_many(json_util.loads(path.read_text(), json_options=options))


def test_sample_data_analytics(client, sample_data):
    assert monthly(client, "EMP0001").json()["attendance_pct"] == 8.7  # 2 / 23
    assert monthly(client, "EMP0002").json()["working_days"] == 15  # joined Mon 13 July
    summary = {i["department"]: i for i in client.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]}
    assert summary["Engineering"]["avg_work_hours"] == 9.04  # (9.12 + 9.03 + 8.98) / 3
    assert (summary["Sales"]["present_days"], summary["Sales"]["avg_work_hours"], summary["Sales"]["leave_count"]) == (1.5, 6.29, 1)
    assert (summary["Support"]["present_days"], summary["Support"]["avg_work_hours"]) == (2.0, 8.75)
    assert rows(leaderboard(client)) == [(1, "EMP0003", 35, 1)]


def test_sample_regularized_record_serializes_history_as_millis(client, sample_data):
    def millis(iso):
        return int(datetime.fromisoformat(iso).timestamp() * 1000)

    item = client.get("/attendance", params={"emp_code": "EMP0001", "date_from": "2026-07-07", "date_to": "2026-07-07"}).json()["items"][0]
    entry = item["history"][0]
    assert entry["changes"]["punch_in"] == {"from": millis("2026-07-07T04:35:00+00:00"), "to": millis("2026-07-07T03:58:00+00:00")}
    assert entry["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert entry["at"] == millis("2026-07-08T05:00:00+00:00")


# --------------------------------------------------------------------------- explain
EXPLAIN_CASES = {
    "attendance_list": {},
    "employee_monthly": {"emp_code": "EMP1001", "month": "2026-07"},
    "department_summary": {"month": "2026-07"},
    "late_leaderboard": {"month": "2026-07"},
    "department_trend": {"department": "Eng", "from": "2026-07-01", "to": "2026-07-31"},
}


@pytest.mark.parametrize("endpoint", EXPLAIN_CASES)
def test_explain_uses_indexes(client, dataset, endpoint):
    r = client.get(f"/admin/explain/{endpoint}", params=EXPLAIN_CASES[endpoint])
    assert r.status_code == 200
    body = r.json()
    assert body["endpoint"] == endpoint and body["collection"] in ("employees", "attendance_logs")
    plan = str(body["explain"])
    assert "IXSCAN" in plan and "COLLSCAN" not in plan


@pytest.mark.parametrize(
    "params",
    [{"emp_code": "EMP1001", "status": "LEAVE"}, {"date_from": "2026-07-01", "date_to": "2026-07-31"}, {"status": "PRESENT", "page": 2}],
)
def test_explain_attendance_list_with_filters(client, dataset, params):
    plan = str(client.get("/admin/explain/attendance_list", params=params).json()["explain"])
    assert "IXSCAN" in plan and "COLLSCAN" not in plan


def test_explain_validation(client, dataset):
    assert client.get("/admin/explain/employee_monthly", params={"month": "2026-07"}).status_code == 422
    assert client.get("/admin/explain/department_trend", params={"department": "Eng"}).status_code == 422
    assert client.get("/admin/explain/late_leaderboard").status_code == 422
    assert client.get("/admin/explain/something_else").status_code == 422


# --------------------------------------------------------------------------- inputs at the edge of the calendar
@pytest.mark.parametrize(
    "path, params",
    [
        ("/analytics/employees/EMP1001/monthly", {"month": "0000-01"}),
        ("/analytics/departments/summary", {"month": "0000-12"}),
        ("/analytics/leaderboard/late", {"month": "0000-06"}),
        ("/admin/explain/employee_monthly", {"emp_code": "EMP1001", "month": "0000-01"}),
    ],
)
def test_month_in_year_zero_is_422_not_500(client, dataset, path, params):
    assert client.get(path, params=params).status_code == 422


def test_trend_ending_on_the_last_representable_day(client, dataset):
    r = trend(client, "Eng", "9999-12-30", "9999-12-31")
    assert r.status_code == 200 and [i["date"] for i in r.json()["items"]] == ["9999-12-30", "9999-12-31"]
    assert client.get("/admin/explain/department_trend", params={"department": "Eng", "from": "9999-12-01", "to": "9999-12-31"}).status_code == 200


def test_moving_average_uses_the_rounded_daily_rates(client, dataset):
    # 07-16: half day for 1 of 3 -> 0.5/3 = 0.16666.. reported as 0.1667; 07-17: 0.0.
    # Mean of the reported rates: (0.1667 + 0) / 2 = 0.08335 -> 0.0834. Mean of exact rates would be 1/12 -> 0.0833.
    items = trend(client, "Eng", "2026-07-16", "2026-07-17").json()["items"]
    assert [i["attendance_rate"] for i in items] == [0.1667, 0.0]
    assert [i["moving_avg_7d"] for i in items] == [0.1667, 0.0834]
