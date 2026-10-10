"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

The contract is openapi.yaml (paths, status codes, business rules R1-R10); what is stored in MongoDB is
DATA_MODEL.md. Defects found in the original starter are listed in REVIEW.md.
"""
import calendar
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, Strict, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.database import Database
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables that are already set in the environment

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

GRACE = timedelta(minutes=10)
MIN_OVERTIME = timedelta(minutes=30)
MAX_SHIFT_LENGTH = timedelta(hours=24)
HALF_DAY_BELOW = Decimal("4.50")
MAX_TREND_DAYS = 92

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
NO_PUNCH_STATUSES = ("ABSENT", "LEAVE")


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def ensure_indexes(db: Database) -> None:
    """Idempotent: create_index is a no-op when an identical index already exists. Reasons in DECISIONS.md."""
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index("joined_on")
    # One record per employee and attendance day; also serves per-employee date ranges and $lookup joins.
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    # Unfiltered / date-filtered listing in its contract sort order; month-wide analytics.
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)])
    # Punch-out: the employee's latest record with punch_in <= the punch-out instant.
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    uri, db_name = os.getenv("MONGO_URI"), os.getenv("MONGO_DB")
    if not uri or not db_name:
        raise RuntimeError("MONGO_URI and MONGO_DB must be set (environment or .env)")
    # tz_aware: read BSON dates back as aware UTC datetimes instead of naive ones.
    client = MongoClient(uri, tz_aware=True, tzinfo=UTC, serverSelectionTimeoutMS=5000)
    app.state.db = client[db_name]
    ensure_indexes(app.state.db)
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def get_db(request: Request) -> Database:
    return request.app.state.db


DB = Annotated[Database, Depends(get_db)]


@app.exception_handler(ConnectionFailure)
def database_unavailable(request: Request, exc: ConnectionFailure) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


def unprocessable(loc: list, msg: str) -> HTTPException:
    """A business-rule 422 shaped like FastAPI's own validation errors, as the contract's ValidationError requires."""
    return HTTPException(status_code=422, detail=[{"loc": loc, "msg": msg, "type": "value_error"}])


# --------------------------------------------------------------------------- #
# Time and business-rule helpers (R1-R5)
# --------------------------------------------------------------------------- #
def from_millis(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def to_millis(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: BSON dates are UTC even when a driver hands them back naive
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def instant_or_now(ms: Optional[int]) -> datetime:
    """A punch instant: the client's value if given, otherwise the server clock; whole seconds either way (R1)."""
    return from_millis(ms) if ms is not None else datetime.now(UTC).replace(microsecond=0)


def parse_hhmm(value: str) -> time:
    hours, minutes = map(int, value.split(":"))
    return time(hours, minutes)


def is_overnight(emp: dict) -> bool:
    return parse_hhmm(emp["shift_end"]) <= parse_hhmm(emp["shift_start"])


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: the IST date of the punch-in; for overnight shifts, before shift_end means the previous day's shift."""
    local = punch_in.astimezone(IST)
    if is_overnight(emp) and local.time() < parse_hhmm(emp["shift_end"]):
        return local.date() - timedelta(days=1)
    return local.date()


def shift_bounds(day: date, emp: dict) -> tuple[datetime, datetime]:
    start = datetime.combine(day, parse_hhmm(emp["shift_start"]), IST)
    end = datetime.combine(day, parse_hhmm(emp["shift_end"]), IST)
    if is_overnight(emp):
        end += timedelta(days=1)
    return start, end


def late_minutes(punch_in: datetime, day: date, emp: dict) -> int:
    """R2: late only when strictly more than 10:00 after shift start; then floor minutes since shift start."""
    shift_start, _ = shift_bounds(day, emp)
    elapsed = punch_in - shift_start
    return elapsed // timedelta(minutes=1) if elapsed > GRACE else 0


def overtime_minutes(punch_out: datetime, day: date, emp: dict) -> int:
    """R3: floor minutes after shift end, counted only when at least 30."""
    _, shift_end = shift_bounds(day, emp)
    extra = punch_out - shift_end
    return extra // timedelta(minutes=1) if extra >= MIN_OVERTIME else 0


def work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: hours rounded to 2 decimals half-up. Decimal, because float round() is not half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / 3600, 2)


def round_half_up(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def derived_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], day: date, emp: dict) -> dict:
    """The four computed fields for a record, following the table in DATA_MODEL.md section 3."""
    if status in NO_PUNCH_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    fields = {"work_hours": None, "late_minutes": late_minutes(punch_in, day, emp), "overtime_minutes": 0, "half_day": False}
    if punch_out is not None:
        hours = work_hours(punch_in, punch_out)
        fields.update(work_hours=float(hours), overtime_minutes=overtime_minutes(punch_out, day, emp), half_day=hours < HALF_DAY_BELOW)
    return fields


def working_days(first: date, last: date) -> int:
    """R7: Monday-Friday days in [first, last], no holiday calendar."""
    return sum(1 for n in range((last - first).days + 1) if (first + timedelta(days=n)).weekday() < 5)


def month_bounds(month: str) -> tuple[date, date]:
    year, mon = map(int, month.split("-"))
    return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #
def _check_calendar_date(value: Optional[str]) -> Optional[str]:
    if value is not None:
        date.fromisoformat(value)  # rejects impossible dates such as 2026-02-30
    return value


def _check_month(value: Optional[str]) -> Optional[str]:
    if value is not None:
        month_bounds(value)  # the pattern allows year 0000, which has no calendar dates
    return value


DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"

DateStr = Annotated[str, Field(pattern=DATE_PATTERN), AfterValidator(_check_calendar_date)]
# Strict: floats, numeric strings and booleans are rejected; the range rejects seconds-looking values.
EpochMillis = Annotated[int, Strict(), Field(ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

DateQuery = Annotated[Optional[str], Query(pattern=DATE_PATTERN), AfterValidator(_check_calendar_date)]
MonthQuery = Annotated[str, Query(pattern=MONTH_PATTERN), AfterValidator(_check_month)]
# Upper bound only so that (page - 1) * page_size always fits MongoDB's signed 64-bit skip.
PageQuery = Annotated[int, Query(ge=1, le=2**63 // 100)]
PageSizeQuery = Annotated[int, Query(ge=1, le=100)]
LimitQuery = Annotated[int, Query(ge=1, le=50)]


def _reject_explicit_null(value: Any) -> Any:
    """Field validators do not run for omitted fields, so this only rejects an explicit JSON null."""
    if value is None:
        raise ValueError("must not be null")
    return value


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: DateStr

    @model_validator(mode="after")
    def shift_has_length(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"

    _no_null = field_validator("punched_at", "status", mode="before")(_reject_explicit_null)


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None

    _no_null = field_validator("punched_at", mode="before")(_reject_explicit_null)


class RegularizeRequest(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    _no_null = field_validator("status", "punch_in", "punch_out", mode="before")(_reject_explicit_null)


# --------------------------------------------------------------------------- #
# Serialization (MongoDB document -> API shape)
# --------------------------------------------------------------------------- #
INSTANT_FIELDS = ("punch_in", "punch_out")


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_millis(doc.get("created_at")),
    }


def history_entry_out(entry: dict) -> dict:
    changes = {}
    for field, change in entry["changes"].items():
        if field in INSTANT_FIELDS:
            change = {"from": to_millis(change["from"]), "to": to_millis(change["to"])}
        changes[field] = change
    return {"at": to_millis(entry["at"]), "by": entry["by"], "reason": entry["reason"], "changes": changes}


def record_out(doc: dict) -> dict:
    """Missing optional fields get the defaults DATA_MODEL.md specifies (legacy records omit some)."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_millis(doc.get("punch_in")),
        "punch_out": to_millis(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": doc.get("half_day") or False,
        "history": [history_entry_out(e) for e in doc.get("history") or []],
    }


def find_employee(db: Database, emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# System and employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health(db: DB):
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable") from None
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate, db: DB):
    doc = body.model_dump() | {"created_at": datetime.now(UTC)}
    try:
        db.employees.insert_one(doc)  # the unique index on emp_code decides concurrent duplicates
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return employee_out(doc)


@app.get("/employees")
def list_employees(db: DB, department: Optional[str] = None, page: PageQuery = 1, page_size: PageSizeQuery = 20):
    query = {} if department is None else {"department": department}
    total = db.employees.count_documents(query)
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(e) for e in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest, db: DB):
    emp = find_employee(db, body.emp_code)
    punched_at = instant_or_now(body.punched_at)
    day = attendance_date(punched_at, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        **derived_fields(body.status, punched_at, None, day, emp),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent insert wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date") from None
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest, db: DB):
    emp = find_employee(db, body.emp_code)
    punched_at = instant_or_now(body.punched_at)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}}, sort=[("punch_in", DESCENDING)]
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if punched_at == record["punch_in"]:
        raise unprocessable(["body", "punched_at"], "punch-out must be after punch-in")
    if punched_at - record["punch_in"] > MAX_SHIFT_LENGTH:
        raise unprocessable(["body", "punched_at"], "punch-out is more than 24 hours after punch-in")

    day = date.fromisoformat(record["date"])
    updates = {"punch_out": punched_at, **derived_fields(record["status"], record["punch_in"], punched_at, day, emp)}
    # Conditional on punch_out still being null, so of two concurrent punch-outs only one matches.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None}, {"$set": updates}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    db: DB,
    emp_code: Optional[str] = None,
    date_from: DateQuery = None,
    date_to: DateQuery = None,
    status: Optional[Status] = None,
    page: PageQuery = 1,
    page_size: PageSizeQuery = 20,
):
    query, sort, skip = attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(query)
    cursor = db.attendance_logs.find(query, sort=sort, skip=skip, limit=page_size)
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


def attendance_list_query(emp_code, date_from, date_to, status, page, page_size) -> tuple[dict, list, int]:
    if date_from and date_to and date_from > date_to:
        raise unprocessable(["query", "date_from"], "date_from must not be after date_to")
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    sort = [("date", DESCENDING), ("emp_code", ASCENDING)]
    return query, sort, (page - 1) * page_size


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    record_date: Annotated[str, Path(alias="date", pattern=DATE_PATTERN), AfterValidator(_check_calendar_date)],
    body: RegularizeRequest,
    db: DB,
):
    emp = find_employee(db, emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": record_date})
    if record is None:
        raise HTTPException(404, "no attendance record for that date")

    before = {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes") or 0,
        "overtime_minutes": record.get("overtime_minutes") or 0,
        "half_day": record.get("half_day") or False,
    }
    after = corrected_state(before, body, date.fromisoformat(record_date), emp)
    changes = {f: {"from": before[f], "to": after[f]} for f in before if before[f] != after[f]}
    if not changes:
        raise unprocessable(["body"], "request changes nothing")

    entry = {"at": datetime.now(UTC), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    history_len = len(record.get("history") or [])
    # Optimistic concurrency: every correction appends one history entry, so the history length acts as a version.
    # Matching it (and the punch-out state, which punch-out changes without touching history) means a concurrent
    # edit makes this update miss instead of being overwritten.
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": before["punch_out"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_len]},
        },
        {"$set": after, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was changed by another request; reload and retry")
    return record_out(updated)


def corrected_state(before: dict, body: RegularizeRequest, day: date, emp: dict) -> dict:
    """Apply the requested corrections, enforce the PATCH rules, and recompute the derived fields."""
    status = body.status or before["status"]
    if status in NO_PUNCH_STATUSES:
        if body.punch_in is not None or body.punch_out is not None:
            raise unprocessable(["body"], f"{status} clears punch times; do not send punch_in or punch_out")
        new_in = new_out = None
    else:
        new_in = from_millis(body.punch_in) if body.punch_in is not None else before["punch_in"]
        new_out = from_millis(body.punch_out) if body.punch_out is not None else before["punch_out"]
        if new_in is None:
            raise unprocessable(["body", "punch_in"], f"status {status} requires a punch_in")
        if attendance_date(new_in, emp) != day:
            raise unprocessable(["body", "punch_in"], "punch_in must stay on the record's attendance date")
        if new_out is not None and not (new_in < new_out <= new_in + MAX_SHIFT_LENGTH):
            raise unprocessable(["body", "punch_out"], "punch_out must be after punch_in and within 24 hours")
    return {"status": status, "punch_in": new_in, "punch_out": new_out, **derived_fields(status, new_in, new_out, day, emp)}


# --------------------------------------------------------------------------- #
# Aggregation building blocks
# --------------------------------------------------------------------------- #
def round_half_up_expr(expr: Any, places: int) -> dict:
    """Half-up rounding in Decimal128. MongoDB's $round rounds half to even, which R8 rules out.
    floor(x + 0.5) is half-up only for x >= 0; every value rounded here is non-negative."""
    factor = Decimal128(str(10**places))
    shifted = {"$add": [{"$multiply": [{"$toDecimal": expr}, factor]}, Decimal128("0.5")]}
    return {"$divide": [{"$floor": shifted}, factor]}


def to_double(expr: Any) -> dict:
    return {"$toDouble": expr}


IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
IS_PRESENT = {"$in": ["$status", list(PRESENCE_STATUSES)]}
PRESENCE_WEIGHT = {"$cond": [IS_PRESENT, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}  # R5, R6
IS_LATE = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}


def count_if(condition: Any) -> dict:
    return {"$sum": {"$cond": [condition, 1, 0]}}


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": {"$cond": [IS_WEEKDAY, PRESENCE_WEIGHT, 0]}},  # R7: Mon-Fri only
                "leave_days": count_if({"$eq": ["$status", "LEAVE"]}),
                "late_count": count_if(IS_LATE),
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthQuery, db: DB):
    emp = find_employee(db, emp_code)
    first, last = month_bounds(month)
    days = working_days(max(first, date.fromisoformat(emp["joined_on"])), last)  # R7: from joined_on
    totals = next(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)), None) or {}
    present = Decimal(str(totals.get("present_days", 0)))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": days,
        "present_days": float(round_half_up(present, 2)),
        "leave_days": totals.get("leave_days", 0),
        "late_count": totals.get("late_count", 0),
        "total_late_minutes": totals.get("total_late_minutes", 0),
        "total_overtime_minutes": totals.get("total_overtime_minutes", 0),
        "attendance_pct": float(round_half_up(present / days * 100, 2)) if days else None,
    }


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    """Starts from employees so that people with no logs still count towards headcount (R9)."""
    first, last = month_bounds(month)
    match: dict = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department
    per_employee_stats = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": {"$cond": [IS_WEEKDAY, PRESENCE_WEIGHT, 0]}},
                "work_hours_sum": {"$sum": {"$cond": [{"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}, {"$toDecimal": "$work_hours"}, 0]}},
                "work_hours_records": count_if({"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}),
                "late_count": count_if(IS_LATE),
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": count_if({"$eq": ["$status", "LEAVE"]}),
                "on_duty_count": count_if({"$eq": ["$status", "ON_DUTY"]}),
            }
        },
    ]
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "pipeline": per_employee_stats, "as": "stats"}},
        {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$stats.present_days"},
                "work_hours_sum": {"$sum": "$stats.work_hours_sum"},
                "work_hours_records": {"$sum": "$stats.work_hours_records"},
                "late_count": {"$sum": "$stats.late_count"},
                "total_late_minutes": {"$sum": "$stats.total_late_minutes"},
                "leave_count": {"$sum": "$stats.leave_count"},
                "on_duty_count": {"$sum": "$stats.on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$replaceWith": {
                "department": "$_id",
                "headcount": "$headcount",
                "present_days": to_double(round_half_up_expr("$present_days", 2)),
                # Mean over records, not a mean of per-employee means.
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_records", 0]},
                        to_double(round_half_up_expr({"$divide": ["$work_hours_sum", "$work_hours_records"]}, 2)),
                        None,
                    ]
                },
                "late_count": "$late_count",
                "total_late_minutes": "$total_late_minutes",
                "leave_count": "$leave_count",
                "on_duty_count": "$on_duty_count",
            }
        },
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: MonthQuery, db: DB, department: Optional[str] = None):
    items = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    return {"month": month, "items": items}


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        # Inner join: logs whose employee does not exist drop out at the $unwind.
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        # Standard competition ranking (1, 2, 2, 4); the cut-off is applied to the rank, not to the row count.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$replaceWith": {
                "rank": "$rank",
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": "$total_late_minutes",
                "late_count": "$late_count",
            }
        },
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthQuery, db: DB, limit: LimitQuery = 10, department: Optional[str] = None):
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


def trend_range(date_from: str, date_to: str) -> tuple[date, date]:
    first, last = date.fromisoformat(date_from), date.fromisoformat(date_to)
    if last < first:
        raise unprocessable(["query", "to"], "to must not be before from")
    if (last - first).days + 1 > MAX_TREND_DAYS:
        raise unprocessable(["query", "to"], f"range must not exceed {MAX_TREND_DAYS} days")
    return first, last


def department_trend_pipeline(department: str, first: date, last: date) -> list:
    """One row per calendar day. Each employee contributes a 'joined' event (on joined_on, or on `from` if they
    joined earlier) and one event per log; events are summed per day, missing days are filled with $densify,
    and headcount is the running sum of joined events."""
    lo, hi = first.isoformat(), last.isoformat()
    range_start = datetime.combine(first, time(), UTC)
    # $densify's upper bound is exclusive; one millisecond past `last` includes it without overflowing on 9999-12-31.
    range_end = datetime.combine(last, time(), UTC) + timedelta(milliseconds=1)
    joined_event = {
        "$cond": [
            {"$lte": ["$joined_on", hi]},
            {"date": {"$max": ["$joined_on", lo]}, "joined": 1, "present": 0, "late": 0},
            {"date": lo, "joined": 0, "present": 0, "late": 0},  # joins after the range: counts nowhere
        ]
    }
    log_events = {"$map": {"input": "$logs", "in": {"date": "$$this.date", "joined": 0, "present": "$$this.present", "late": "$$this.late"}}}
    rate = {
        "$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            round_half_up_expr({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4),
            None,
        ]
    }
    return [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": lo, "$lte": hi}}},
                    {"$project": {"_id": 0, "date": 1, "present": PRESENCE_WEIGHT, "late": {"$cond": [IS_LATE, 1, 0]}}},
                ],
                "as": "logs",
            }
        },
        {"$project": {"_id": 0, "event": {"$concatArrays": [[joined_event], log_events]}}},
        {"$unwind": "$event"},
        {"$group": {"_id": "$event.date", "joined": {"$sum": "$event.joined"}, "present": {"$sum": "$event.present"}, "late": {"$sum": "$event.late"}}},
        {"$project": {"_id": 0, "day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}, "joined": 1, "present": 1, "late": 1}},
        {
            "$densify": {
                "field": "day",
                "range": {"step": 1, "unit": "day", "bounds": [range_start, range_end]},
            }
        },
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"headcount": {"$sum": {"$ifNull": ["$joined", 0]}, "window": {"documents": ["unbounded", "current"]}}}}},
        {
            "$set": {
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
                "present_count": {"$ifNull": ["$present", 0]},
                "late_count": {"$ifNull": ["$late", 0]},
            }
        },
        {"$set": {"attendance_rate": rate}},
        # $avg skips nulls and returns null when the window has no numbers; the window cannot reach before `from`
        # because no rows exist there.
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"day": 1}},
        {
            "$replaceWith": {
                "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
                "is_working_day": "$is_working_day",
                "headcount": "$headcount",
                "present_count": {"$toDouble": "$present_count"},
                "late_count": "$late_count",
                "attendance_rate": {"$cond": [{"$eq": ["$attendance_rate", None]}, None, to_double("$attendance_rate")]},
                "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_7d", None]}, None, to_double(round_half_up_expr("$moving_avg_7d", 4))]},
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: Annotated[str, Query(alias="from", pattern=DATE_PATTERN), AfterValidator(_check_calendar_date)],
    date_to: Annotated[str, Query(alias="to", pattern=DATE_PATTERN), AfterValidator(_check_calendar_date)],
    db: DB,
):
    first, last = trend_range(date_from, date_to)
    # Checked separately: $densify emits the full date range even when no employee matches.
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    items = list(db.employees.aggregate(department_trend_pipeline(department, first, last)))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def require(params: dict) -> None:
    missing = [name for name, value in params.items() if value is None]
    if missing:
        raise HTTPException(422, detail=[{"loc": ["query", name], "msg": "Field required", "type": "missing"} for name in missing])


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    db: DB,
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_PATTERN), AfterValidator(_check_month)] = None,
    department: Optional[str] = None,
    limit: LimitQuery = 10,
    date_from: DateQuery = None,
    date_to: DateQuery = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[str], Query(alias="from", pattern=DATE_PATTERN), AfterValidator(_check_calendar_date)] = None,
    to: DateQuery = None,
    page: PageQuery = 1,
    page_size: PageSizeQuery = 20,
):
    """Builds the operation with the same function the endpoint uses, then explains it with executionStats."""
    if endpoint == "attendance_list":
        query, sort, skip = attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command = {"find": collection, "filter": query, "sort": dict(sort), "skip": skip, "limit": page_size}
    else:
        if endpoint == "employee_monthly":
            require({"emp_code": emp_code, "month": month})
            collection, pipeline = "attendance_logs", employee_monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            require({"month": month})
            collection, pipeline = "employees", department_summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            require({"month": month})
            collection, pipeline = "attendance_logs", late_leaderboard_pipeline(month, limit, department)
        else:
            require({"department": department, "from": from_, "to": to})
            first, last = trend_range(from_, to)
            collection, pipeline = "employees", department_trend_pipeline(department, first, last)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    result = db.command("explain", command, verbosity="executionStats")
    # Explain output contains BSON types (Timestamp, Int64, ...); relaxed Extended JSON makes it plain JSON.
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))}
