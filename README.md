# Employee Attendance & Analytics API (HROne Assignment)

A high-performance FastAPI and MongoDB-backed REST API for managing employee attendance, punch-ins/outs, manual regularizations (with full audit trail history), and real-time attendance analytics via optimized MongoDB aggregation pipelines.

## Stack
- **Language**: Python 3.11+
- **Framework**: FastAPI (Uvicorn)
- **Database**: MongoDB 6.0+ (PyMongo)
- **Data Serialization & Validation**: Pydantic v2

---

## Key Features

1. **Employee Management**:
   - Unique employee code registration (`emp_code`).
   - Shift start/end configurations with support for standard and overnight shifts.

2. **Attendance Operations**:
   - **Punch-In**: Auto-calculates IST attendance date (R1) and late minutes (R2). Prevents concurrent duplicate punches via compound unique DB indexes.
   - **Punch-Out**: Auto-calculates work hours (R4), overtime minutes (R3), and half-day status (R5). Race-safe against simultaneous punch-outs.
   - **Attendance Regularization (PATCH)**: Manual corrections recalculate all derived fields and append a immutable audit trail entry (`history`) detailing changed fields (`from` / `to`).

3. **Analytics (MongoDB Aggregation Pipelines)**:
   - **Monthly Employee Summary**: `present_days`, `leave_days`, `late_count`, `total_late_minutes`, `total_overtime_minutes`, `attendance_pct`.
   - **Department Summary**: Department headcount (including employees with zero logs), average work hours, leave/on-duty counts.
   - **Late Leaders Leaderboard**: Standard competition ranking (1, 2, 2, 4) using `$setWindowFields` / `$rank`, handling ties gracefully.
   - **Department Daily Trend**: Full date gap filling (`from` to `to`) with 7-day moving average calculation.

4. **Performance & Query Optimization**:
   - Idempotent index creation at startup.
   - `/admin/explain/{endpoint}` endpoint providing raw execution stats showing `IXSCAN` utilization without collection scans (`COLLSCAN`).

---

## Setup & Running Locally

### 1. Environment Setup
```bash
python -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configuration (`.env`)
Set environment variables or create a `.env` file:
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

### 3. Seed Sample Data
```bash
python sample_seed.py
```

### 4. Run Server
```bash
uvicorn app.main:app --port 8000 --reload
```

---

## API Endpoints Overview

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Readiness probe (checks MongoDB ping) |
| `POST` | `/employees` | Register new employee |
| `GET` | `/employees` | List employees (paginated, department filter) |
| `POST` | `/attendance/punch-in` | Record punch-in |
| `POST` | `/attendance/punch-out` | Record punch-out |
| `GET` | `/attendance` | List attendance logs (paginated, filtered) |
| `PATCH` | `/attendance/{emp_code}/{date}` | Regularize attendance with audit trail |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly employee attendance summary |
| `GET` | `/analytics/departments/summary` | Department summary |
| `GET` | `/analytics/leaderboard/late` | Late leaderboard with tie ranking |
| `GET` | `/analytics/departments/{department}/trend` | Daily department attendance trend & 7-day moving avg |
| `GET` | `/admin/explain/{endpoint}` | Execution stats & index usage explain plan |

---

## Documentation Files
- [`REVIEW.md`](REVIEW.md): Detailed review of defects found in starter code and how they were resolved.
- [`DECISIONS.md`](DECISIONS.md): Architectural decisions covering indexes, race condition handling, ranking ties, headcount logic, and 100x scaling strategy.
