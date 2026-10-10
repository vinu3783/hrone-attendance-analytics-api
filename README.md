# Employee Attendance & Analytics API

A FastAPI + MongoDB service for the HROne engineering assignment. Employees punch in and out, HR corrects records with
an append-only audit trail, and four analytics endpoints answer attendance questions with MongoDB aggregation pipelines.

The contract is [`openapi.yaml`](openapi.yaml) (endpoints, status codes, business rules R1-R10). What is stored in
MongoDB is described in [`DATA_MODEL.md`](DATA_MODEL.md). The task description (`PROBLEM_STATEMENT.docx`) belongs to
HROne and is not published in this repository.

- Code review of the starter: [REVIEW.md](REVIEW.md)
- Design decisions: [DECISIONS.md](DECISIONS.md)

## Contents

- [Status](#status)
- [Repository layout](#repository-layout)
- [Setup](#setup)
- [Running](#running)
- [Endpoints](#endpoints)
- [Examples](#examples)
- [Business rules as implemented](#business-rules-as-implemented)
- [Corrections and audit history](#corrections-and-audit-history)
- [Analytics](#analytics)
- [Indexes](#indexes)
- [Tests](#tests)
- [Interpretations of ambiguous points](#interpretations-of-ambiguous-points)
- [Known limitations](#known-limitations)

## Status

Every endpoint in `openapi.yaml` is implemented. Nothing in the contract is knowingly missing.

What has been verified, and where:

- **Tests:** the full suite passes on Python 3.11.9 with MongoDB 7.0.14 (local, Windows).
- **Contract:** the generated OpenAPI paths, methods and parameters match `openapi.yaml`. Every documented
  (operation, status code) pair was produced against a running server, and each response body was checked against its
  `openapi.yaml` schema.
- **Scale:** a manual check against a generated dataset of 100,671 attendance records. Every explain plan used `IXSCAN`
  and none used `COLLSCAN`. Latencies measured from a local HTTP client (persistent connection, same machine) were 5-80 ms for
  most endpoints and about 250 ms for the department summary.
- **Not tested:** MongoDB 6.0, Atlas, and Linux/macOS hosts.

## Repository layout

| Path | Purpose |
|---|---|
| `app/main.py` | The whole application, as the assignment requires a single file. |
| `tests/` | Pytest suite: pure rule tests and integration tests against a live MongoDB. |
| `openapi.yaml`, `DATA_MODEL.md` | Assignment materials (unchanged). |
| `sample_data/`, `sample_seed.py` | Nine sample documents and a loader (unchanged). |
| `REVIEW.md`, `DECISIONS.md` | Part A review and Part C answers. |
| `requirements.txt` | Runtime dependencies. |
| `requirements-dev.txt` | Runtime plus test dependencies. |
| `.env.example` | Template for local configuration. |

## Setup

Prerequisites: Python 3.11+ and MongoDB 6.0+ (the grader uses 7.0). This can be a local install, Docker, or an Atlas M0
cluster.

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt     # or requirements.txt if you will not run the tests
cp .env.example .env                    # then edit MONGO_URI / MONGO_DB
```

**Windows PowerShell**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

### Configuration

| Variable | Required | Meaning |
|---|---|---|
| `MONGO_URI` | yes | MongoDB connection string, e.g. `mongodb://localhost:27017` or an Atlas `mongodb+srv://` URI. |
| `MONGO_DB` | yes | Database name. |

These are read from the environment. A `.env` file in the repository root is loaded for convenience, but real
environment variables take priority. There are no built-in defaults: the app refuses to start if either variable is
missing, so it can never silently use the wrong database. Never commit `.env`; it is in `.gitignore`.

### MongoDB

Any MongoDB 6.0+ works. Locally with Docker: `docker run -d -p 27017:27017 mongo:7`. No manual setup is needed: the
app creates its indexes at startup, and creating them again is a no-op.

### Sample data

```bash
python sample_seed.py    # replaces the contents of employees and attendance_logs in MONGO_DB
```

This loads 6 employees and 9 attendance records. It **deletes** the existing documents in both collections first.

## Running

```bash
uvicorn app.main:app --port 8000
```

- Swagger UI: http://localhost:8000/docs
- Generated OpenAPI: http://localhost:8000/openapi.json

`GET /health` returns 200 once MongoDB answers a ping, and 503 otherwise. Startup connects to MongoDB and creates the
indexes, so the server will not start if MongoDB is unreachable at that moment (it gives up after 5 seconds).

## Endpoints

All instants in requests and responses are integer **epoch milliseconds**. Dates are `YYYY-MM-DD` (IST), months are
`YYYY-MM`. Errors use FastAPI's `{"detail": ...}` body. A 422 always carries a list of error objects, including for
business-rule violations.

| Method | Path | Purpose | Main responses |
|---|---|---|---|
| GET | `/health` | Readiness: pings MongoDB. | 200, 503 |
| POST | `/employees` | Create an employee (`emp_code`, `name`, `email`, `department`, `joined_on`; optional `shift_start` / `shift_end`, default 09:30-18:30). | 201, 409 duplicate `emp_code`, 422 |
| GET | `/employees` | List, sorted by `emp_code`; `department`, `page`, `page_size` filters. | 200, 422 |
| POST | `/attendance/punch-in` | `emp_code`, optional `punched_at` (default now), optional `status` (`PRESENT`, `WFH` or `ON_DUTY`). | 201, 404 unknown employee, 409 already punched in that day, 422 |
| POST | `/attendance/punch-out` | `emp_code`, optional `punched_at`. Closes the latest record whose punch-in is at or before `punched_at`. | 200, 404 no employee / no punch-in, 409 already punched out, 422 not after punch-in or more than 24 h later |
| GET | `/attendance` | List, sorted by `date` desc then `emp_code`; filters `emp_code`, `date_from`, `date_to`, `status`, `page`, `page_size`. | 200, 422 (including `date_from` > `date_to`) |
| PATCH | `/attendance/{emp_code}/{date}` | Correct one record: optional `status`, `punch_in`, `punch_out`; required `reason` (5-200 chars) and `regularized_by`. | 200, 404, 409 concurrent edit, 422 |
| GET | `/analytics/employees/{emp_code}/monthly?month=` | Monthly summary for one employee. | 200, 404, 422 |
| GET | `/analytics/departments/summary?month=[&department=]` | One row per department. | 200, 422 |
| GET | `/analytics/leaderboard/late?month=[&limit=&department=]` | Late-comers ranking (`limit` 1-50, default 10). | 200, 422 |
| GET | `/analytics/departments/{department}/trend?from=&to=` | One row per day, at most 92 days. | 200, 404 unknown department, 422 |
| GET | `/admin/explain/{endpoint}` | MongoDB `explain` (`executionStats`) of an endpoint's main query. Takes the same query parameters as that endpoint. | 200, 422 |

`{endpoint}` for explain is one of `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`,
`department_trend`.

## Examples

These run against the sample data (`python sample_seed.py`). Each one was executed against this implementation.

```bash
curl -s localhost:8000/health
# {"status":"ok"}

curl -s -X POST localhost:8000/employees -H 'Content-Type: application/json' \
  -d '{"emp_code":"EMP0007","name":"Divya Menon","email":"divya@example.com","department":"Sales","joined_on":"2026-07-01"}'
# {"emp_code":"EMP0007",...,"shift_start":"09:30","shift_end":"18:30","joined_on":"2026-07-01","created_at":<epoch ms>}

# 2026-07-15 09:41:30 IST: 11 min 30 s after a 09:30 shift start, so late_minutes = 11
curl -s -X POST localhost:8000/attendance/punch-in -H 'Content-Type: application/json' \
  -d '{"emp_code":"EMP0002","punched_at":1784088690000}'
# {"emp_code":"EMP0002","date":"2026-07-15","status":"PRESENT","punch_in":1784088690000,"punch_out":null,
#  "work_hours":null,"late_minutes":11,"overtime_minutes":0,"half_day":false,"history":[]}

# 18:02:10 IST, 8 h 20 min 40 s later
curl -s -X POST localhost:8000/attendance/punch-out -H 'Content-Type: application/json' \
  -d '{"emp_code":"EMP0002","punched_at":1784118730000}'
# {...,"punch_out":1784118730000,"work_hours":8.34,"late_minutes":11,"overtime_minutes":0,"half_day":false,...}

curl -s 'localhost:8000/attendance?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-31&page=1&page_size=20'

# Correct the 2026-07-14 punch-in to 09:20 IST. Derived fields are recomputed and one history entry is appended.
curl -s -X PATCH localhost:8000/attendance/EMP0002/2026-07-14 -H 'Content-Type: application/json' \
  -d '{"punch_in":1784001000000,"reason":"biometric glitch","regularized_by":"hr.admin"}'
# {...,"work_hours":9.18,"history":[{"at":<epoch ms>,"by":"hr.admin","reason":"biometric glitch",
#   "changes":{"punch_in":{"from":1784001720000,"to":1784001000000},"work_hours":{"from":8.98,"to":9.18}}}]}

curl -s 'localhost:8000/analytics/employees/EMP0001/monthly?month=2026-07'
# {"emp_code":"EMP0001","month":"2026-07","working_days":23,"present_days":2.0,"leave_days":0,"late_count":0,
#  "total_late_minutes":0,"total_overtime_minutes":0,"attendance_pct":8.7}

curl -s 'localhost:8000/analytics/departments/summary?month=2026-07'
curl -s 'localhost:8000/analytics/leaderboard/late?month=2026-07&limit=10'
curl -s 'localhost:8000/analytics/departments/Sales/trend?from=2026-07-01&to=2026-07-31'
curl -s 'localhost:8000/admin/explain/department_trend?department=Sales&from=2026-07-01&to=2026-07-31'
```

In Windows PowerShell, call `curl.exe` (not the `curl` alias) and escape the inner double quotes, or use Swagger UI.

## Business rules as implemented

| Rule | Implementation (`app/main.py`) |
|---|---|
| R1 time zone and date | `attendance_date()`: the IST date of the punch. For an overnight shift (`shift_end <= shift_start`), a punch before `shift_end` belongs to the previous day. `from_millis()` truncates every incoming instant to whole seconds; that value is stored and returned. |
| R2 late | `late_minutes()`: late only when strictly more than 10:00 after shift start, then floor minutes since shift start. |
| R3 overtime | `overtime_minutes()`: floor minutes after shift end (next day for overnight shifts), 0 unless at least 30. |
| R4 work hours | `work_hours()`: `Decimal` seconds / 3600, `ROUND_HALF_UP` to 2 places. Float `round()` gives 8.04 for 8.045 h. |
| R5 half day | `work_hours` after rounding < 4.50. So 4 h 29 min 42 s rounds to 4.50 and is not a half day. |
| R6-R7 presence, working days | `PRESENT` / `WFH` / `ON_DUTY` count, half days as 0.5, Mon-Fri only. Working days are counted from `max(month start, joined_on)`. |
| R8 rounding | Half-up in Decimal128 inside the pipelines (`round_half_up_expr`). MongoDB's `$round` rounds half to even, which R8 rules out. |
| R9 headcount | Employees with `joined_on` on or before the end of the period, logs or not. |
| R10 pagination | `page` >= 1; `page_size` 1-100, default 20; `total` counts the filtered set. `page` is capped at 2^63 / 100 so the skip offset always fits MongoDB's 64-bit integer; larger values are 422 instead of a 500. |

Concurrency, verified by the parallel tests in `tests/test_attendance_api.py`:

- **Punch-in:** the unique `(emp_code, date)` index means exactly one of many identical requests gets 201.
- **Punch-out:** the update only matches while `punch_out` is still null, so exactly one request gets 200.
- **Employee creation:** the unique `emp_code` index gives one 201 and 409 for the rest.

## Corrections and audit history

`PATCH /attendance/{emp_code}/{date}`:

1. Applies the requested `status`, `punch_in` and `punch_out` to the stored record. Omitted fields keep their stored
   values.
2. Enforces the contract's rules, returning 422 when one fails:
   - `ABSENT` or `LEAVE` clears both punches, and sending a punch with them is 422.
   - A presence status needs a `punch_in`.
   - `punch_in` must keep the same attendance date (R1).
   - `punch_out` must be after `punch_in` and at most 24 h later.
3. Recomputes the four derived fields from the final times.
4. Builds `changes` from every field whose value actually differs, including derived fields. If nothing differs, the
   result is 422.
5. Writes the new values and **appends** `{at, by, reason, changes}` to `history` in one atomic single-document update.
   History entries are never modified. In MongoDB, `from` / `to` of `punch_in` and `punch_out` are BSON dates; the API
   returns them as epoch ms.

Concurrent corrections use optimistic concurrency. Each correction adds exactly one history entry, so the update is
conditional on the history length (and `punch_out`) still being what was read. A concurrent edit makes the slower
request match nothing and return 409, so no history entry is lost or based on stale data. No multi-document
transaction is needed, because the record and its history are one document.

## Analytics

All four analytics read the **stored** derived values; none recompute them from punch times. Each is a single
aggregation pipeline.

- **Employee monthly** runs on `attendance_logs`:
  1. `$match` on `emp_code` and the month's date range (uses the `(emp_code, date)` index).
  2. `$group`, where `present_days` counts weekday presence records and half days as 0.5.

  Working days come from the calendar in Python, because there is nothing to aggregate. `attendance_pct` is computed
  half-up from those two numbers.
- **Department summary** runs on `employees`:
  1. Filter employees with `joined_on` on or before the month end.
  2. `$lookup` that month's logs, already grouped per employee inside the lookup sub-pipeline.
  3. `$unwind` with `preserveNullAndEmptyArrays`, so employees without logs remain.
  4. `$group` by department.

  `avg_work_hours` is total work hours divided by the number of presence records that have work hours, so it is a mean
  over records rather than a mean of per-employee means.
- **Late leaderboard** runs on `attendance_logs`:
  1. `$match` the month's records with `late_minutes > 0`.
  2. Group by employee.
  3. Inner `$lookup` to `employees`, which drops logs whose employee does not exist, then the optional department
     filter.
  4. `$setWindowFields` with `$rank` (standard competition ranking: 1, 2, 2, 4).
  5. `$match rank <= limit`, then sort by total desc and `emp_code`.
- **Department trend** runs on `employees`:
  1. Each employee emits a "joined" event (on `joined_on`, or on `from` if they joined earlier) plus one event per log
     in the range.
  2. Events are summed per day.
  3. `$densify` fills every missing calendar day.
  4. A running `$sum` window turns joined events into the daily headcount.
  5. `attendance_rate` is computed for working days with headcount > 0.
  6. A `[-6, 0]` document window `$avg` gives the 7-day moving average. `$avg` skips nulls, and the window cannot reach
     before `from` because no rows exist there.

## Indexes

Created by `ensure_indexes()` at startup:

| Collection | Index | Serves |
|---|---|---|
| employees | `emp_code` (unique) | Lookups by code; duplicate creates give 409; leaderboard `$lookup`. |
| employees | `department, emp_code` | Department-filtered employee list in order; trend; department existence check. |
| employees | `joined_on` | Department summary headcount filter. |
| attendance_logs | `emp_code, date` (unique) | One record per employee per day; PATCH lookup; monthly range; `$lookup` joins from employees. |
| attendance_logs | `date desc, emp_code` | Attendance list sort order and date filters; month-wide leaderboard scan. |
| attendance_logs | `status, date desc, emp_code` | Status-filtered list and its `total` count. |
| attendance_logs | `emp_code, punch_in desc` | Punch-out: latest record with `punch_in <= punched_at`. |

`GET /admin/explain/{endpoint}` builds its query with the same function the endpoint uses. For the summary and trend,
the explained pipeline runs on `employees`. The output's `$lookup` stage reports `indexesUsed: ["emp_code_1_date_1"]`
and `collectionScans: 0` for the join into `attendance_logs`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest                                                   # uses mongodb://localhost:27017
TEST_MONGO_URI="mongodb://host:port" pytest              # any other MongoDB 6.0+
```

```powershell
$env:TEST_MONGO_URI = "mongodb://localhost:27017"; pytest
```

How the suite is organised:

- **Isolation:** tests that need MongoDB use a fresh database named `hrone_test_<random>`, dropped at the end. They
  never touch `MONGO_DB`.
- **Without MongoDB:** if MongoDB is unreachable, those tests are **skipped** (reported as skipped, not passed) and only
  the pure rule tests in `tests/test_rules.py` run.

What the files cover:

- **`test_rules.py`:** R1-R5 and R7 edge cases. Examples: 09:40:00 vs 09:40:01, overnight dates, the 30-minute
  overtime threshold, the 8.045 h half-up case, and the 4.495 h half-day boundary.
- **`test_attendance_api.py`:**
  - the contract's status codes for these endpoints (the concurrent-correction test accepts 409 but cannot force
    one; that 409 was reproduced manually with parallel requests);
  - strict epoch-ms validation;
  - legacy records without `history` / `half_day`;
  - parallel punch-ins, punch-outs, creates and corrections;
  - 503 when the database is unreachable.
- **`test_analytics.py`:** analytics on a hand-built dataset with expected values worked out by hand in the comments.
  It covers:
  - ties at the cutoff;
  - employees with no logs;
  - a mid-month joiner and a future joiner;
  - weekend records;
  - a legacy record and logs for a non-existent employee;
  - a mean over records that differs from a mean of means;
  - a half-up case where `$round` would differ.

  It also checks the supplied sample data and asserts `IXSCAN` and no `COLLSCAN` for every explain target.

## Interpretations of ambiguous points

These are the choices I made where the contract leaves room. Each is easy to change.

- **Trend range limit:** "exceeds 92 days" is read as more than 92 calendar days inclusive. 2026-07-01 to 2026-09-30
  (92 days) is accepted; one more day is 422.
- **Moving average:** it averages the daily `attendance_rate` values as reported, already rounded to 4 places.
- **Punch-out at the 24-hour mark:** exactly 24 h after punch-in is allowed; 24 h + 1 s is 422. When the latest record
  is already closed, 409 is returned before any time check.
- **"A request that changes nothing":** evaluated after recomputation, so a request that only refreshes stale derived
  values on a seeded record counts as a change and is recorded in history.
- **Explicit `null`:** `null` for `punched_at`, `status`, `punch_in` or `punch_out` is 422. The schema does not allow it,
  and for PATCH it would be ambiguous between "clear" and "unchanged". Omitting the field is the way to leave it unset.
- **Database outages:** an outage after startup returns 503 on any endpoint rather than 500, and the app recovers on
  its own when MongoDB returns (checked by stopping and restarting MongoDB under a running server).
- **Records before `joined_on`:** they still count in analytics, because no rule excludes them. My test data has no
  such records, so how the hidden dataset treats them is unverified.

## Known limitations

- **No authentication:** `regularized_by` is free text, as the assignment specifies.
- **Deep pagination:** `/attendance` and `/employees` page with `skip`, which costs O(offset). Keyset pagination on
  `(date, emp_code)` would scale better.
- **Department summary cost:** it runs one indexed `$lookup` per employee, about 250 ms for 500 employees and 100k
  logs on a laptop. DECISIONS.md describes the rollup approach for larger data.
- **Startup requires MongoDB:** the app does not retry at startup. If MongoDB is down, the server exits instead of
  serving 503s until the database comes back.
- **Verified environment only:** Python 3.11.9 and MongoDB 7.0.14 on Windows.
