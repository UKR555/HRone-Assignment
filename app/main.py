"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
from datetime import datetime, timedelta, timezone, date as date_type
from decimal import Decimal, ROUND_HALF_UP
import math
import os
import re
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# IST Timezone (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))

mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
mongo_db_name = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(mongo_uri, tz_aware=True)
db = client[mongo_db_name]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def init_db_indexes():
    try:
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
        db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)])

        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
        db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)])
    except Exception as e:
        print(f"Index initialization warning: {e}")


init_db_indexes()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def round_half_up_num(val: Optional[float], decimals: int) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    fmt = "0." + "0" * decimals if decimals > 0 else "0"
    return float(d.quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def parse_epoch_ms(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, bool) or not isinstance(ts, int):
        raise HTTPException(422, "Timestamp must be an integer count of milliseconds")
    if ts < 100000000000 or ts > 4102444800000:
        raise HTTPException(422, "Timestamp out of valid epoch milliseconds range")
    # Truncate to whole seconds per R1
    sec = ts // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def parse_time_str(t_str: str) -> tuple[int, int]:
    h, m = map(int, t_str.split(":"))
    return h, m


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def get_attendance_date_ist(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """R1: Computes the IST attendance date (YYYY-MM-DD) for a punch-in instant."""
    punch_in_ist = punch_in_utc.astimezone(IST)
    p_date = punch_in_ist.date()
    p_time_str = punch_in_ist.strftime("%H:%M")

    if is_overnight_shift(shift_start, shift_end):
        if p_time_str < shift_end:
            return (p_date - timedelta(days=1)).isoformat()
    return p_date.isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    """R2: Grace period 10 min. Late only if strictly > 10:00 past shift start."""
    sh, sm = parse_time_str(shift_start)
    y, m, d = map(int, attendance_date.split("-"))
    shift_start_ist = datetime(y, m, d, sh, sm, 0, tzinfo=IST)
    punch_in_ist = punch_in_utc.astimezone(IST)

    diff_sec = (punch_in_ist - shift_start_ist).total_seconds()
    if diff_sec > 600:  # strictly more than 10 mins (600 s)
        return math.floor(diff_sec / 60.0)
    return 0


def compute_work_hours(punch_in_utc: Optional[datetime], punch_out_utc: Optional[datetime]) -> Optional[float]:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    if not punch_out_utc or not punch_in_utc:
        return None
    sec = (punch_out_utc - punch_in_utc).total_seconds()
    raw = sec / 3600.0
    return round_half_up_num(raw, 2)


def compute_overtime_minutes(
    punch_out_utc: Optional[datetime], shift_start: str, shift_end: str, attendance_date: str
) -> int:
    """R3: Floor mins from shift_end to punch_out, counted only if >= 30 mins."""
    if not punch_out_utc:
        return 0
    eh, em = parse_time_str(shift_end)
    y, m, d = map(int, attendance_date.split("-"))
    shift_end_date = date_type(y, m, d)
    if is_overnight_shift(shift_start, shift_end):
        shift_end_date += timedelta(days=1)

    shift_end_ist = datetime(shift_end_date.year, shift_end_date.month, shift_end_date.day, eh, em, 0, tzinfo=IST)
    punch_out_ist = punch_out_utc.astimezone(IST)

    diff_sec = (punch_out_ist - shift_end_ist).total_seconds()
    if diff_sec >= 1800:  # >= 30 mins
        return math.floor(diff_sec / 60.0)
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: half_day = true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    res = dict(doc)
    res.pop("_id", None)
    res["created_at"] = dt_to_epoch_ms(res.get("created_at"))
    return res


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    res = dict(doc)
    res.pop("_id", None)
    res.pop("id", None)
    res["punch_in"] = dt_to_epoch_ms(res.get("punch_in"))
    res["punch_out"] = dt_to_epoch_ms(res.get("punch_out"))
    
    history_formatted = []
    for h in res.get("history", []):
        h_copy = dict(h)
        h_copy["at"] = dt_to_epoch_ms(h_copy.get("at"))
        changes_copy = {}
        for k, v in h_copy.get("changes", {}).items():
            if k in ("punch_in", "punch_out"):
                changes_copy[k] = {
                    "from": dt_to_epoch_ms(v.get("from")) if v.get("from") else None,
                    "to": dt_to_epoch_ms(v.get("to")) if v.get("to") else None,
                }
            else:
                changes_copy[k] = dict(v)
        h_copy["changes"] = changes_copy
        history_formatted.append(h_copy)
    
    res["history"] = history_formatted
    return res


def get_month_date_range(month_str: str) -> tuple[str, str, int]:
    try:
        y, m = map(int, month_str.split("-"))
        first_day = date_type(y, m, 1)
        if m == 12:
            next_first = date_type(y + 1, 1, 1)
        else:
            next_first = date_type(y, m + 1, 1)
        last_day = next_first - timedelta(days=1)
        return first_day.isoformat(), last_day.isoformat(), last_day.day
    except Exception:
        raise HTTPException(422, "Invalid month format. Expected YYYY-MM")


def count_working_days(from_date_str: str, to_date_str: str) -> int:
    """Counts Mon-Fri working days between from_date and to_date inclusive."""
    d_start = date_type.fromisoformat(from_date_str)
    d_end = date_type.fromisoformat(to_date_str)
    if d_start > d_end:
        return 0
    cur = d_start
    cnt = 0
    while cur <= d_end:
        if cur.weekday() < 5:  # Mon-Fri
            cnt += 1
        cur += timedelta(days=1)
    return cnt


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(...)


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")

    doc = body.model_dump()
    # Server clock creation time truncated to whole seconds in UTC
    sec = int(datetime.now(timezone.utc).timestamp())
    doc["created_at"] = datetime.fromtimestamp(sec, tz=timezone.utc)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, f"emp_code '{body.emp_code}' already exists")

    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort([("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [format_employee_doc(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, f"Employee '{body.emp_code}' not found")

    if body.status not in ("PRESENT", "WFH", "ON_DUTY"):
        raise HTTPException(422, f"Invalid status '{body.status}' for punch-in")

    if body.punched_at is not None:
        punch_in_utc = parse_epoch_ms(body.punched_at)
    else:
        sec = int(datetime.now(timezone.utc).timestamp())
        punch_in_utc = datetime.fromtimestamp(sec, tz=timezone.utc)

    attendance_date = get_attendance_date_ist(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_utc, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, f"Employee '{body.emp_code}' is already punched in for date {attendance_date}")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, f"Employee '{body.emp_code}' not found")

    if body.punched_at is not None:
        punch_out_utc = parse_epoch_ms(body.punched_at)
    else:
        sec = int(datetime.now(timezone.utc).timestamp())
        punch_out_utc = datetime.fromtimestamp(sec, tz=timezone.utc)

    # Find employee's most recent record whose punch_in <= punched_at
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", DESCENDING)],
    )

    if not rec or rec.get("punch_in") is None:
        raise HTTPException(404, f"No open punch-in record found for employee '{body.emp_code}'")

    if rec.get("punch_out") is not None:
        raise HTTPException(409, "Attendance record is already punched out")

    punch_in_utc = rec["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    if diff_sec <= 0:
        raise HTTPException(422, "punched_at must be strictly after punch_in")
    if diff_sec > 86400:  # 24 hours
        raise HTTPException(422, "punched_at cannot be more than 24 hours after punch_in")

    attendance_date = rec["date"]
    work_hrs = compute_work_hours(punch_in_utc, punch_out_utc)
    overtime_mins = compute_overtime_minutes(punch_out_utc, emp["shift_start"], emp["shift_end"], attendance_date)
    half_day_flag = compute_half_day(work_hrs)

    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hrs,
                "overtime_minutes": overtime_mins,
                "half_day": half_day_flag,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(409, "Attendance record is already punched out")

    return format_attendance_doc(updated_doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be greater than date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance_doc(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, f"Employee '{emp_code}' not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, f"No attendance record found for employee '{emp_code}' on date {date}")

    new_status = body.status if body.status is not None else rec["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, f"punch_in and punch_out cannot be provided for status {new_status}")
        final_punch_in = None
        final_punch_out = None
    else:
        if body.punch_in is not None:
            final_punch_in = parse_epoch_ms(body.punch_in)
        else:
            final_punch_in = rec.get("punch_in")
            if final_punch_in and final_punch_in.tzinfo is None:
                final_punch_in = final_punch_in.replace(tzinfo=timezone.utc)

        if not final_punch_in:
            raise HTTPException(422, f"Presence status '{new_status}' requires a valid punch_in")

        # R1: punch_in must stay on the record's attendance date
        r1_date = get_attendance_date_ist(final_punch_in, emp["shift_start"], emp["shift_end"])
        if r1_date != date:
            raise HTTPException(422, f"punch_in instant belongs to attendance date {r1_date}, expected {date}")

        if body.punch_out is not None:
            final_punch_out = parse_epoch_ms(body.punch_out)
        else:
            final_punch_out = rec.get("punch_out")
            if final_punch_out and final_punch_out.tzinfo is None:
                final_punch_out = final_punch_out.replace(tzinfo=timezone.utc)

        if final_punch_out is not None:
            diff_sec = (final_punch_out - final_punch_in).total_seconds()
            if diff_sec <= 0:
                raise HTTPException(422, "punch_out must be strictly after punch_in")
            if diff_sec > 86400:
                raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        new_late_mins = 0
        new_work_hrs = None
        new_overtime_mins = 0
        new_half_day = False
    else:
        new_late_mins = compute_late_minutes(final_punch_in, emp["shift_start"], date)
        if final_punch_out is None:
            new_work_hrs = None
            new_overtime_mins = 0
            new_half_day = False
        else:
            new_work_hrs = compute_work_hours(final_punch_in, final_punch_out)
            new_overtime_mins = compute_overtime_minutes(final_punch_out, emp["shift_start"], emp["shift_end"], date)
            new_half_day = compute_half_day(new_work_hrs)

    # Compute actual changes
    changes = {}
    if new_status != rec.get("status"):
        changes["status"] = {"from": rec.get("status"), "to": new_status}

    old_pi = rec.get("punch_in")
    if old_pi and old_pi.tzinfo is None:
        old_pi = old_pi.replace(tzinfo=timezone.utc)
    if old_pi != final_punch_in:
        changes["punch_in"] = {"from": old_pi, "to": final_punch_in}

    old_po = rec.get("punch_out")
    if old_po and old_po.tzinfo is None:
        old_po = old_po.replace(tzinfo=timezone.utc)
    if old_po != final_punch_out:
        changes["punch_out"] = {"from": old_po, "to": final_punch_out}

    if new_work_hrs != rec.get("work_hours"):
        changes["work_hours"] = {"from": rec.get("work_hours"), "to": new_work_hrs}

    if new_late_mins != rec.get("late_minutes", 0):
        changes["late_minutes"] = {"from": rec.get("late_minutes", 0), "to": new_late_mins}

    if new_overtime_mins != rec.get("overtime_minutes", 0):
        changes["overtime_minutes"] = {"from": rec.get("overtime_minutes", 0), "to": new_overtime_mins}

    if new_half_day != rec.get("half_day", False):
        changes["half_day"] = {"from": rec.get("half_day", False), "to": new_half_day}

    if not changes:
        raise HTTPException(422, "Regularization request changes nothing on the attendance record")

    # Audit trail entry
    now_sec = int(datetime.now(timezone.utc).timestamp())
    history_entry = {
        "at": datetime.fromtimestamp(now_sec, tz=timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    current_history = rec.get("history", [])
    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "history": {"$size": len(current_history)}},
        {
            "$set": {
                "status": new_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": new_work_hrs,
                "late_minutes": new_late_mins,
                "overtime_minutes": new_overtime_mins,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(409, "Concurrent modification detected on attendance record history")

    return format_attendance_doc(updated_doc)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (MongoDB Aggregation Pipelines)
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, f"Employee '{emp_code}' not found")

    month_start, month_end, _ = get_month_date_range(month)
    joined_on = emp["joined_on"]

    # Calculate working days per R7
    if joined_on > month_end:
        working_days = 0
    else:
        calc_start = joined_on if joined_on > month_start else month_start
        working_days = count_working_days(calc_start, month_end)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "half_day": 1,
                "date_obj": {"$dateFromString": {"dateString": "$date"}},
            }
        },
        {
            "$project": {
                "status": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "half_day": 1,
                "day_of_week": {"$dayOfWeek": "$date_obj"},  # 1=Sun, 2=Mon, ..., 6=Fri, 7=Sat
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},  # Mon-Fri only
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        data = res[0]
        present_days = round_half_up_num(data.get("present_days", 0.0), 2)
        leave_days = data.get("leave_days", 0)
        late_count = data.get("late_count", 0)
        total_late = data.get("total_late_minutes", 0)
        total_ot = data.get("total_overtime_minutes", 0)
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days == 0:
        attendance_pct = None
    else:
        raw_pct = (present_days / working_days) * 100.0
        attendance_pct = round_half_up_num(raw_pct, 4)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary_analytics(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    month_start, month_end, _ = get_month_date_range(month)

    match_emp: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        match_emp["department"] = department

    pipeline = [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"e_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$e_code"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "has_log": {"$cond": [{"$ifNull": ["$logs._id", False]}, 1, 0]},
                "log_status": "$logs.status",
                "log_half_day": "$logs.half_day",
                "log_work_hours": "$logs.work_hours",
                "log_late_minutes": "$logs.late_minutes",
                "log_date_obj": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$dateFromString": {"dateString": "$logs.date"}},
                        None,
                    ]
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "has_log": 1,
                "log_status": 1,
                "log_half_day": 1,
                "log_work_hours": 1,
                "log_late_minutes": 1,
                "day_of_week": {
                    "$cond": [{"$ifNull": ["$log_date_obj", False]}, {"$dayOfWeek": "$log_date_obj"}, None]
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees_set": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$log_status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$log_half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "total_work_hours": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$log_status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$log_work_hours", None]},
                                ]
                            },
                            "$log_work_hours",
                            0.0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$log_status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$log_work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$log_late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$cond": [{"$gt": ["$log_late_minutes", 0]}, "$log_late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$log_status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$log_status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {"$sort": {"_id": ASCENDING}},
    ]

    raw_items = list(db.employees.aggregate(pipeline))
    items = []
    for row in raw_items:
        headcount = len(row.get("employees_set", []))
        if headcount == 0:
            continue
        p_days = round_half_up_num(row.get("present_days", 0.0), 2)
        wh_count = row.get("work_hours_count", 0)
        if wh_count > 0:
            raw_avg = row.get("total_work_hours", 0.0) / float(wh_count)
            avg_wh = round_half_up_num(raw_avg, 2)
        else:
            avg_wh = None

        items.append(
            {
                "department": row["_id"],
                "headcount": headcount,
                "present_days": p_days,
                "avg_work_hours": avg_wh,
                "late_count": row.get("late_count", 0),
                "total_late_minutes": row.get("total_late_minutes", 0),
                "leave_count": row.get("leave_count", 0),
                "on_duty_count": row.get("on_duty_count", 0),
            }
        )

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard_analytics(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    month_start, month_end, _ = get_month_date_range(month)

    pipeline = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": DESCENDING},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        ]
    )

    raw_items = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for r in raw_items:
        items.append(
            {
                "rank": r["rank"],
                "emp_code": r["_id"],
                "name": r["emp"]["name"],
                "department": r["emp"]["department"],
                "total_late_minutes": r["total_late_minutes"],
                "late_count": r["late_count"],
            }
        )

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend_analytics(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    if to_date < from_date:
        raise HTTPException(422, "'to' date cannot be earlier than 'from' date")

    d_from_dt = date_type.fromisoformat(from_date)
    d_to_dt = date_type.fromisoformat(to_date)
    num_days = (d_to_dt - d_from_dt).days + 1
    if num_days > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    dept_emp = db.employees.find_one({"department": department})
    if not dept_emp:
        raise HTTPException(404, f"Department '{department}' not found")

    d_from_utc = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    d_to_next_utc = (datetime.fromisoformat(to_date) + timedelta(days=1)).replace(tzinfo=timezone.utc)

    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"department": 1, "date_obj": {"$dateFromString": {"dateString": from_date}}}},
        {
            "$densify": {
                "field": "date_obj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [d_from_utc, d_to_next_utc],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_obj"}},
                "date_obj": 1,
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"target_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$target_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "hc"},
                ],
                "as": "hc_res",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"target_date": "$date"},
                "pipeline": [
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp",
                        }
                    },
                    {"$unwind": "$emp"},
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$target_date"]},
                                    {"$eq": ["$emp.department", department]},
                                ]
                            }
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "date": 1,
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc_res.hc", 0]}, 0]},
                "logs": 1,
                "day_of_week": {"$dayOfWeek": "$date_obj"},
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},  # Mon-Fri
                "headcount": 1,
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": ASCENDING}},
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
    ]

    raw_items = list(db.employees.aggregate(pipeline))
    items = []
    for r in raw_items:
        ar = round_half_up_num(r.get("attendance_rate"), 4)
        ma = round_half_up_num(r.get("moving_avg_7d"), 4)
        items.append(
            {
                "date": r["date"],
                "is_working_day": r["is_working_day"],
                "headcount": r["headcount"],
                "present_count": round_half_up_num(r.get("present_count", 0.0), 2),
                "late_count": r["late_count"],
                "attendance_rate": ar,
                "moving_avg_7d": ma,
            }
        )

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., enum=["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from cannot be greater than date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to

        sort_doc = [("date", DESCENDING), ("emp_code", ASCENDING)]
        skip = (page - 1) * page_size
        explain_cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": dict(sort_doc),
            "skip": skip,
            "limit": page_size,
        }
        res = db.command("explain", explain_cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly explain")
        month_start, month_end, _ = get_month_date_range(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
            {"$project": {"date": 1, "status": 1, "late_minutes": 1, "overtime_minutes": 1, "half_day": 1}},
        ]
        res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary explain")
        month_start, month_end, _ = get_month_date_range(month)
        match_emp: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
        if department:
            match_emp["department"] = department
        pipeline = [
            {"$match": match_emp},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"e_code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$e_code"]},
                                        {"$gte": ["$date", month_start]},
                                        {"$lte": ["$date", month_end]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
        ]
        res = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": res}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard explain")
        month_start, month_end, _ = get_month_date_range(month)
        pipeline = [
            {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        ]
        res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required for department_trend explain")
        if to_date < from_date:
            raise HTTPException(422, "'to' date cannot be earlier than 'from' date")
        d_from_utc = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
        d_to_next_utc = (datetime.fromisoformat(to_date) + timedelta(days=1)).replace(tzinfo=timezone.utc)
        pipeline = [
            {"$match": {"department": department}},
            {"$limit": 1},
            {"$project": {"department": 1, "date_obj": {"$dateFromString": {"dateString": from_date}}}},
            {
                "$densify": {
                    "field": "date_obj",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [d_from_utc, d_to_next_utc],
                    },
                }
            },
        ]
        res = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": res}

