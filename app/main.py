"""
Employee Attendance & Analytics API - FIXED VERSION

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, List

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, OperationFailure

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# =========================================================================== #
# Startup: create indexes idempotently
# =========================================================================== #
@app.on_event("startup")
def ensure_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)])


# =========================================================================== #
# Time helpers
# =========================================================================== #
def now_utc_naive() -> datetime:
    """Current UTC time as a naive datetime (PyMongo's default shape)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> naive UTC datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Naive UTC (or aware) datetime -> epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def to_ist(dt: datetime) -> datetime:
    """Naive UTC datetime -> IST-aware datetime."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def truncate_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def half_up(value: float, digits: int) -> float:
    """Round half-up to `digits` decimals."""
    q = Decimal(1).scaleb(-digits)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


# =========================================================================== #
# Business helpers (R1 - R5)
# =========================================================================== #
def is_overnight(shift_start: str, shift_end: str) -> bool:
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    return (eh, em) <= (sh, sm)


def compute_attendance_date(ist_dt: datetime, shift_start: str, shift_end: str) -> str:
    """R1: return YYYY-MM-DD attendance date for a punch-in instant (IST-aware)."""
    if is_overnight(shift_start, shift_end):
        eh, em = map(int, shift_end.split(":"))
        t = ist_dt.time()
        if (t.hour, t.minute, t.second) < (eh, em, 0):
            return (ist_dt.date() - timedelta(days=1)).isoformat()
    return ist_dt.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, date_str: str) -> int:
    """R2: strictly more than 10 min after shift start, then floor elapsed minutes.
    The shift start is built on the record's ATTENDANCE date (R1), so overnight
    shifts compare against the correct calendar day."""
    h, m = map(int, shift_start.split(":"))
    y, mo, d = map(int, date_str.split("-"))
    start = datetime(y, mo, d, h, m, 0, tzinfo=IST)
    p = to_ist(punch_in_utc)
    seconds = (p - start).total_seconds()
    if seconds <= 600:
        return 0
    return int(seconds // 60)


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: hours, rounded half-up to 2 decimals."""
    hours = (punch_out_utc - punch_in_utc).total_seconds() / 3600
    return half_up(hours, 2)


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """R3: whole minutes past shift_end, only if >= 30. Overnight aware."""
    eh, em = map(int, shift_end.split(":"))
    y, mo, d = map(int, date_str.split("-"))
    end = datetime(y, mo, d, eh, em, 0, tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    out = to_ist(punch_out_utc)
    minutes = int((out - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


# =========================================================================== #
# Serialization helpers
# =========================================================================== #
def employee_to_json(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


def _convert_changes(changes: dict) -> dict:
    out = {}
    for field, pair in (changes or {}).items():
        frm, to = pair.get("from"), pair.get("to")
        if field in ("punch_in", "punch_out"):
            frm = dt_to_ms(frm) if frm is not None else None
            to = dt_to_ms(to) if to is not None else None
        out[field] = {"from": frm, "to": to}
    return out


def record_to_json(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [
            {
                "at": dt_to_ms(h["at"]),
                "by": h["by"],
                "reason": h["reason"],
                "changes": _convert_changes(h.get("changes", {})),
            }
            for h in (doc.get("history") or [])
        ],
    }


# =========================================================================== #
# Models
# =========================================================================== #
EMP_CODE_RE = r"^EMP\d{4,6}$"
EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
HHMM_RE = r"^([01]\d|2[0-3]):[0-5]\d$"
DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
MS_MIN = 100000000000
MS_MAX = 4102444800000


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=EMP_CODE_RE)
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=EMAIL_RE)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_RE)
    shift_end: str = Field(default="18:30", pattern=HHMM_RE)
    joined_on: str = Field(pattern=DATE_RE)

    @model_validator(mode="after")
    def check_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=MS_MIN, le=MS_MAX)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=MS_MIN, le=MS_MAX)


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[int] = Field(default=None, ge=MS_MIN, le=MS_MAX)
    punch_out: Optional[int] = Field(default=None, ge=MS_MIN, le=MS_MAX)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# =========================================================================== #
# System
# =========================================================================== #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# =========================================================================== #
# Employees
# =========================================================================== #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc_naive()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_to_json(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [employee_to_json(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# =========================================================================== #
# Attendance: punch-in / punch-out / list / regularize
# =========================================================================== #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc_naive()
    ts = truncate_seconds(ts)
    ist_dt = to_ist(ts)
    att_date = compute_attendance_date(ist_dt, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(ts, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_to_json(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc_naive()
    ts = truncate_seconds(ts)

    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}, "punch_out": None},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(404, "no open punch-in found")

    pi = rec["punch_in"]
    if pi.tzinfo is not None:
        pi = pi.replace(tzinfo=None)
    delta = (ts - pi).total_seconds()
    if delta <= 0:
        raise HTTPException(422, "punched_at must be after punch_in")
    if delta > 24 * 3600:
        raise HTTPException(422, "punched_at more than 24 hours after punch_in")

    wh = compute_work_hours(pi, ts)
    ot = compute_overtime(ts, emp["shift_start"], emp["shift_end"], rec["date"])
    hd = wh < 4.5

    result = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": wh,
            "overtime_minutes": ot,
            "half_day": hd,
        }},
        return_document=True,
    )
    if not result:
        raise HTTPException(409, "already punched out")
    return record_to_json(result)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and not re.match(DATE_RE, date_from):
        raise HTTPException(422, "invalid date_from")
    if date_to and not re.match(DATE_RE, date_to):
        raise HTTPException(422, "invalid date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [record_to_json(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{att_date}")
def regularize(emp_code: str, att_date: str, body: RegularizeIn):
    if not re.match(DATE_RE, att_date):
        raise HTTPException(422, "invalid date")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": att_date})
    if not rec:
        raise HTTPException(404, "no record for that date")

    new_status = body.status if body.status is not None else rec["status"]
    new_pi = rec.get("punch_in")
    new_po = rec.get("punch_out")
    if body.punch_in is not None:
        new_pi = truncate_seconds(ms_to_dt(body.punch_in))
    if body.punch_out is not None:
        new_po = truncate_seconds(ms_to_dt(body.punch_out))

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "cannot supply punch times with ABSENT/LEAVE")
        new_pi = None
        new_po = None
    else:
        if new_pi is None:
            raise HTTPException(422, "presence status requires punch_in")

    if new_pi is not None:
        expected = compute_attendance_date(to_ist(new_pi), emp["shift_start"], emp["shift_end"])
        if expected != att_date:
            raise HTTPException(422, "punch_in does not fall on the record's date")

    if new_po is not None and new_pi is not None:
        d = (new_po - new_pi).total_seconds()
        if d <= 0:
            raise HTTPException(422, "punch_out must be after punch_in")
        if d > 24 * 3600:
            raise HTTPException(422, "punch_out more than 24 hours after punch_in")

    new_late = compute_late_minutes(new_pi, emp["shift_start"], att_date) if new_pi else 0
    if new_pi is not None and new_po is not None:
        new_wh = compute_work_hours(new_pi, new_po)
        new_ot = compute_overtime(new_po, emp["shift_start"], emp["shift_end"], att_date)
        new_hd = new_wh < 4.5
    else:
        new_wh, new_ot, new_hd = None, 0, False

    changes: dict = {}
    if new_status != rec["status"]:
        changes["status"] = {"from": rec["status"], "to": new_status}
    old_pi, old_po = rec.get("punch_in"), rec.get("punch_out")
    if old_pi != new_pi:
        changes["punch_in"] = {"from": old_pi, "to": new_pi}
    if old_po != new_po:
        changes["punch_out"] = {"from": old_po, "to": new_po}
    if rec.get("late_minutes", 0) != new_late:
        changes["late_minutes"] = {"from": rec.get("late_minutes", 0), "to": new_late}
    if rec.get("work_hours") != new_wh:
        changes["work_hours"] = {"from": rec.get("work_hours"), "to": new_wh}
    if rec.get("overtime_minutes", 0) != new_ot:
        changes["overtime_minutes"] = {"from": rec.get("overtime_minutes", 0), "to": new_ot}
    if bool(rec.get("half_day", False)) != bool(new_hd):
        changes["half_day"] = {"from": bool(rec.get("half_day", False)), "to": bool(new_hd)}

    if not changes:
        raise HTTPException(422, "request changes nothing")

    history_entry = {
        "at": now_utc_naive(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    old_len = len(rec.get("history") or [])
    result = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, old_len]},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_pi,
                "punch_out": new_po,
                "work_hours": new_wh,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_hd,
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )
    if not result:
        raise HTTPException(409, "concurrent edit detected")
    return record_to_json(result)


# =========================================================================== #
# Analytics
# =========================================================================== #
def _month_bounds(month: str):
    if not re.match(MONTH_RE, month):
        raise HTTPException(422, "invalid month")
    y, m = map(int, month.split("-"))
    start = date(y, m, 1)
    end = date(y + 1, 1, 1) - timedelta(days=1) if m == 12 else date(y, m + 1, 1) - timedelta(days=1)
    return y, m, start, end


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_RE)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    y, m, mstart, mend = _month_bounds(month)
    prefix = f"{month}-"

    join_d = date.fromisoformat(emp["joined_on"])
    cur = max(mstart, join_d)
    working_days = 0
    while cur <= mend:
        if cur.weekday() < 5:
            working_days += 1
        cur += timedelta(days=1)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{prefix}"}}},
        {"$addFields": {"dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]},
                ]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    r = rows[0] if rows else {
        "present_days": 0, "leave_days": 0, "late_count": 0,
        "total_late_minutes": 0, "total_overtime_minutes": 0,
    }
    if working_days > 0:
        pct = half_up((r["present_days"] / working_days) * 100, 4)
    else:
        pct = None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": r["present_days"],
        "leave_days": r["leave_days"],
        "late_count": r["late_count"],
        "total_late_minutes": r["total_late_minutes"],
        "total_overtime_minutes": r["total_overtime_minutes"],
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=MONTH_RE),
    department: Optional[str] = None,
):
    y, m, mstart, mend = _month_bounds(month)
    ms, me = mstart.isoformat(), mend.isoformat()

    emp_match = {"joined_on": {"$lte": me}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"ec": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$eq": ["$emp_code", "$$ec"]},
                    "date": {"$gte": ms, "$lte": me},
                }},
                {"$addFields": {"dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
            ],
            "as": "logs",
        }},
        {"$group": {
            "_id": "$department",
            "emp_codes": {"$addToSet": "$emp_code"},
            "logs": {"$push": "$logs"},
        }},
        {"$project": {
            "department": "$_id",
            "headcount": {"$size": "$emp_codes"},
            "flat_logs": {"$reduce": {
                "input": "$logs",
                "initialValue": [],
                "in": {"$concatArrays": ["$$value", "$$this"]},
            }},
        }},
        {"$project": {
            "department": 1,
            "headcount": 1,
            "present_days": {"$sum": {"$map": {
                "input": "$flat_logs", "as": "lg",
                "in": {"$cond": [
                    {"$and": [
                        {"$in": ["$$lg.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$gte": ["$$lg.dow", 2]}, {"$lte": ["$$lg.dow", 6]},
                    ]},
                    {"$cond": [{"$ifNull": ["$$lg.half_day", False]}, 0.5, 1]},
                    0,
                ]},
            }}},
            "work_hours_sum": {"$sum": {"$map": {
                "input": "$flat_logs", "as": "lg",
                "in": {"$cond": [
                    {"$and": [
                        {"$in": ["$$lg.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$ne": [{"$ifNull": ["$$lg.work_hours", None]}, None]},
                    ]},
                    "$$lg.work_hours", 0,
                ]},
            }}},
            "work_hours_count": {"$size": {"$filter": {
                "input": "$flat_logs", "as": "lg",
                "cond": {"$and": [
                    {"$in": ["$$lg.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$ne": [{"$ifNull": ["$$lg.work_hours", None]}, None]},
                ]},
            }}},
            "late_count": {"$size": {"$filter": {
                "input": "$flat_logs", "as": "lg",
                "cond": {"$gt": [{"$ifNull": ["$$lg.late_minutes", 0]}, 0]},
            }}},
            "total_late_minutes": {"$sum": {"$map": {
                "input": "$flat_logs", "as": "lg",
                "in": {"$ifNull": ["$$lg.late_minutes", 0]},
            }}},
            "leave_count": {"$size": {"$filter": {
                "input": "$flat_logs", "as": "lg",
                "cond": {"$eq": ["$$lg.status", "LEAVE"]},
            }}},
            "on_duty_count": {"$size": {"$filter": {
                "input": "$flat_logs", "as": "lg",
                "cond": {"$eq": ["$$lg.status", "ON_DUTY"]},
            }}},
        }},
        {"$addFields": {
            "avg_work_hours": {"$cond": [
                {"$gt": ["$work_hours_count", 0]},
                {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                None,
            ]},
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]

    items = []
    for row in db.employees.aggregate(pipeline):
        avg = row.get("avg_work_hours")
        items.append({
            "department": row["department"],
            "headcount": row["headcount"],
            "present_days": row["present_days"],
            "avg_work_hours": half_up(avg, 2) if avg is not None else None,
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    _month_bounds(month)
    prefix = f"{month}-"

    pipeline = [
        {"$match": {
            "date": {"$regex": f"^{prefix}"},
            "late_minutes": {"$gt": 0},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
            "name": {"$first": "$emp.name"},
            "department": {"$first": "$emp.department"},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": 1,
            "department": 1,
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", pattern=DATE_RE),
    to: str = Query(..., pattern=DATE_RE),
):
    f_d = date.fromisoformat(from_)
    t_d = date.fromisoformat(to)
    if t_d < f_d:
        raise HTTPException(422, "to must be >= from")
    if (t_d - f_d).days > 91:
        raise HTTPException(422, "range exceeds 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "unknown department")

    days = []
    cur = f_d
    while cur <= t_d:
        days.append(cur.isoformat())
        cur += timedelta(days=1)

    pipeline = [
        {"$documents": [{"date": d} for d in days]},
        {"$lookup": {
            "from": "employees",
            "let": {"d": "$date"},
            "pipeline": [
                {"$match": {
                    "department": department,
                    "$expr": {"$lte": ["$joined_on", "$$d"]},
                }},
                {"$count": "n"},
            ],
            "as": "hc",
        }},
        {"$addFields": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"d": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$d"]}}},
                {"$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "emp",
                }},
                {"$unwind": "$emp"},
                {"$match": {"emp.department": department}},
            ],
            "as": "day_logs",
        }},
        {"$addFields": {
            "present_count": {"$sum": {"$map": {
                "input": "$day_logs", "as": "lg",
                "in": {"$cond": [
                    {"$in": ["$$lg.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": [{"$ifNull": ["$$lg.half_day", False]}, 0.5, 1]},
                    0,
                ]},
            }}},
            "late_count": {"$size": {"$filter": {
                "input": "$day_logs", "as": "lg",
                "cond": {"$gt": [{"$ifNull": ["$$lg.late_minutes", 0]}, 0]},
            }}},
            "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
        }},
        {"$addFields": {
            "is_working_day": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
        }},
        {"$addFields": {
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": [
                    {"$floor": {"$add": [
                        {"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]},
                        0.5,
                    ]}},
                    10000,
                ]},
                None,
            ]},
        }},
        {"$sort": {"date": 1}},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {"moving_avg_7d": {
                "$avg": "$attendance_rate",
                "window": {"documents": [-6, 0]},
            }},
        }},
        {"$addFields": {
            "moving_avg_7d": {"$cond": [
                {"$eq": ["$moving_avg_7d", None]},
                None,
                {"$divide": [
                    {"$floor": {"$add": [{"$multiply": ["$moving_avg_7d", 10000]}, 0.5]}},
                    10000,
                ]},
            ]},
        }},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": 1, "late_count": 1,
            "attendance_rate": 1, "moving_avg_7d": 1,
        }},
    ]
    items = list(db.aggregate(pipeline))
    return {"department": department, "items": items}


# =========================================================================== #
# Admin: explain
# =========================================================================== #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        cursor = (db.attendance_logs.find(q)
                  .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
                  .skip((page - 1) * page_size).limit(page_size))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": cursor.explain()}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month required")
        prefix = f"{month}-"
        pipe = [
            {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{prefix}"}}},
            {"$addFields": {"dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
            {"$group": {"_id": None,
                        "present_days": {"$sum": 0},
                        "late_count": {"$sum": 0}}},
        ]
        expl = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}},
                          verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": expl}

    if endpoint == "department_summary":
        pipe = [
            {"$match": {"joined_on": {"$lte": "2026-12-31"}}},
            {"$lookup": {"from": "attendance_logs",
                         "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}},
            {"$group": {"_id": "$department", "n": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ]
        expl = db.command("explain", {"aggregate": "employees", "pipeline": pipe, "cursor": {}},
                          verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": expl}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month required")
        prefix = f"{month}-"
        pipe = [
            {"$match": {"date": {"$regex": f"^{prefix}"}, "late_minutes": {"$gt": 0}}},
            {"$lookup": {"from": "employees", "localField": "emp_code",
                         "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
            {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}}},
            {"$sort": {"total": -1, "_id": 1}},
        ]
        expl = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}},
                          verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": expl}

    if endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from, to required")
        pipe = [
            {"$match": {"date": {"$gte": from_, "$lte": to}}},
            {"$lookup": {"from": "employees", "localField": "emp_code",
                         "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
            {"$group": {"_id": "$date", "n": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ]
        expl = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}},
                          verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": expl}

    raise HTTPException(422, "unknown endpoint")