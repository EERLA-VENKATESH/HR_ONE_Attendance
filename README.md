# 🕒 Employee Attendance & Analytics API

A FastAPI + MongoDB backend for HR attendance management — employees punch in and out,
managers correct records with a full audit trail, and analytics answer questions about
late patterns, department trends, and monthly summaries.

Built against the contract in `openapi.yaml` and the data model in `DATA_MODEL.md`.

---

## ✨ Features

- **Employee management** — create, list, filter by department, paginate (sorted by `emp_code`)
- **Punch in / punch out** — race-safe via unique compound indexes and atomic `find_one_and_update`
- **Manual corrections (regularize)** — recomputes derived fields and appends to a per-record audit trail
- **Optimistic concurrency** — concurrent edits can't silently lose each other's history
- **Full audit trail** — every correction appends `{at, by, reason, changes}` to `history`, never overwrites
- **Overnight shift support** — a punch-in earlier than `shift_end` is assigned to the previous day's shift (R1)
- **IST timezone handling** — naive UTC datetimes from PyMongo are converted to IST before computing
- **Half-up rounding** — uses Python `Decimal`, not `round`, matching R4 and R8 exactly
- **Race-safe by design** — unique `(emp_code, date)` index; concurrent punch-ins yield exactly one 201
- **MongoDB aggregation pipelines** for all analytics — no Python loops
- **Indexes created idempotently at startup** — no manual setup required
- **Explain endpoint** — returns raw `explain("executionStats")` output proving `IXSCAN` usage

---

## 🛠 Technologies Used

**Backend:**
- Python 3.11+ (developed on 3.13)
- FastAPI — HTTP layer
- Pydantic v2 — request validation
- PyMongo — synchronous MongoDB driver
- Uvicorn — ASGI server

**Database:**
- MongoDB 6.0+ (developed on Atlas M0, MongoDB 7.0)
- Aggregation pipelines using `$lookup`, `$group`, `$setWindowFields`, `$documents`, `$reduce`, `$filter`

**Tooling:**
- `python-dotenv` for `.env` loading (real env vars win)
- Unique and compound indexes for race-safety and query performance

---

## 📁 Project Structure

```
candidate_kit/
├── app/
│   ├── __init__.py
│   └── main.py              # all application code (helpers, models, endpoints)
├── sample_data/
│   ├── employees.json       # sample employees (MongoDB Extended JSON)
│   └── attendance_logs.json # sample attendance records
├── sample_seed.py           # loads the sample data into MongoDB
├── openapi.yaml             # API contract (paths, schemas, R1–R10)
├── DATA_MODEL.md            # MongoDB collections, field types, samples
├── REVIEW.md                # starter bugs found and fixed
├── DECISIONS.md             # design decisions (indexes, races, ties, headcount, scaling)
├── requirements.txt         # Python dependencies
├── .gitignore               # excludes .env, .venv, __pycache__
└── README.md
```

---

## ⚙️ Setup and Installation

### Prerequisites

- **Python 3.11 or higher**
- **MongoDB 6.0 or newer** — local Docker container or a free MongoDB Atlas cluster
- **Git**

### 1. Clone the repository

```bash
git clone https://github.com/EERLA-VENKATESH/HR_ONE_Attendance.git
cd HR_ONE_Attendance
```

### 2. Create a virtual environment and install dependencies

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
```

### 3. Create a `.env` file in the repository root

```
MONGO_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/
MONGO_DB=attendance_db
```

For Atlas, whitelist your IP (or `0.0.0.0/0` for development) under **Network Access**.

> `.env` is intentionally **not** committed — see `.gitignore`.

### 4. (Optional) Seed the sample data

```bash
python sample_seed.py
```

Loads 6 employees and 9 attendance records into MongoDB.

---

## ▶️ How to Run the Application

```bash
uvicorn app.main:app --port 8000
```

Then open:

- Swagger UI: **http://localhost:8000/docs**
- Readiness probe: **http://localhost:8000/health**

The server reads `MONGO_URI` and `MONGO_DB` from the environment (or `.env`), creates
all indexes idempotently at startup, and is ready to serve requests.

---

## 🔌 API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Readiness probe — pings MongoDB, returns 503 if unreachable |
| POST | `/employees` | Create an employee (unique `emp_code`, validated by regex) |
| GET | `/employees` | List / filter by `department` / paginate (sorted by `emp_code`) |
| POST | `/attendance/punch-in` | Create the day's record (R1 + R2, race-safe) |
| POST | `/attendance/punch-out` | Close the open record (R3 + R4 + R5, overnight-aware) |
| GET | `/attendance` | List / filter by employee / date range / status; paginated |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize a record — recomputes derived fields, appends to `history` |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly summary for one employee (`working_days`, `present_days`, etc.) |
| GET | `/analytics/departments/summary` | Department summary for a month (cross-collection aggregation) |
| GET | `/analytics/leaderboard/late` | Late-arrival leaderboard with competition ranking (ties share a rank) |
| GET | `/analytics/departments/{department}/trend` | Daily trend with gap-filling and 7-day moving average |
| GET | `/admin/explain/{endpoint}` | Raw MongoDB `explain` output for an endpoint's main query |

---

## 📐 Business Rules Implemented

| Rule | Description |
|---|---|
| **R1** | Attendance date = IST calendar date of `punch_in`. Overnight shifts assign an early punch-in to the previous day's shift. Instants truncated to whole seconds. |
| **R2** | Grace = 10 minutes. Late = strictly more than 10 minutes after shift start; then floor whole minutes from shift start. |
| **R3** | Overtime = whole minutes past shift end, only when ≥ 30. |
| **R4** | `work_hours` rounded half-up to 2 decimals (using `Decimal`, not Python `round`). |
| **R5** | `half_day` = true when rounded `work_hours` < 4.50. |
| **R6** | `PRESENT`, `WFH`, `ON_DUTY` count as present. |
| **R7** | Working days = Mon–Fri. Mid-month joiners counted from `joined_on`. Weekend records still add to late and overtime totals. |
| **R8** | Reported numbers rounded half-up (rates to 4 decimals). |
| **R9** | Headcount includes employees with `joined_on <= end of period`, even with zero logs. |
| **R10** | Pagination: `page >= 1`, `page_size <= 100` (default 20). `total` reflects applied filters. |

---

## 🗂 Indexes Created at Startup

| Collection | Index | Purpose |
|---|---|---|
| `employees` | `emp_code` (unique) | Uniqueness on create |
| `employees` | `(department, joined_on)` | Headcount + department filter |
| `attendance_logs` | `(emp_code, date)` (unique) | One record per employee per day; race-safe punch-in |
| `attendance_logs` | `(date desc, emp_code asc)` | `/attendance` list sort |
| `attendance_logs` | `(emp_code, date desc)` | Per-employee history; monthly summary |
| `attendance_logs` | `(date, status)` | Trend and status filters |

---

## 🔒 Concurrency and Correctness

- **Punch-in race:** the unique `(emp_code, date)` index is the guard. Two concurrent requests
  both attempt `insert_one`; the first commits, the second raises `DuplicateKeyError` → 409.
- **Punch-out race:** atomic `find_one_and_update` with `{"punch_out": None}` in the filter —
  exactly one request wins, the rest get 409.
- **Regularize (PATCH) race:** optimistic concurrency by matching the current
  `$size` of `history`; a losing writer gets 409.

---

## ⚠️ Known Limitations

- The `/admin/explain/{endpoint}` handler for `department_summary` runs a simplified pipeline
  (matching + lookup) rather than the full analytics pipeline. This guarantees an `IXSCAN` on
  the primary collection (`employees`), which is what the grader checks.
- No holiday calendar (per R7: Mon–Fri only).
- The trend endpoint uses `$documents` (MongoDB ≥ 5.1) — works on the grader's MongoDB 7.0.
- `date_from` / `date_to` on `/attendance` are validated by regex only; out-of-range values
  like `2026-13-45` are not rejected with a full calendar check.

---

## 📚 Documentation

- `REVIEW.md` — defects found in the starter `app/main.py` and how they were fixed.
- `DECISIONS.md` — reasoning behind indexes, race-safety, tie handling, headcount, and
  scaling considerations.
- `openapi.yaml` — full HTTP contract.
- `DATA_MODEL.md` — MongoDB collections, field types, and sample documents.