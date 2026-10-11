# Employee Attendance & Analytics API

A FastAPI + MongoDB backend for HR attendance: employees punch in and out, managers
correct records with a full audit trail, and analytics answer questions about late
patterns, department trends, and monthly summaries.

Built against the contract in `openapi.yaml` and the data model in `DATA_MODEL.md`.

---

## Tech stack

- **Python 3.11+** (developed on 3.13)
- **FastAPI** for the HTTP layer and Pydantic v2 validation
- **PyMongo** (synchronous driver) for MongoDB
- **MongoDB 6.0+** (developed on Atlas, MongoDB 7.0)
- **Uvicorn** as the ASGI server

---

## Highlights

- **12 endpoints** covering CRUD, punch-in/punch-out, corrections, four analytics
  endpoints, and a MongoDB explain endpoint.
- **Race-safe** punch-in / punch-out using unique compound indexes and atomic
  `find_one_and_update`.
- **Optimistic concurrency** on the PATCH (regularize) endpoint via history-length
  guard — concurrent edits can't silently overwrite each other's audit trail.
- **MongoDB aggregation pipelines** for all analytics (no Python loops).
- **Indexes created idempotently** at startup — no manual setup.
- **Business rules R1–R10** implemented, including overnight shifts, IST timezone
  handling, half-up rounding, and competition ranking with ties.

---

## Run

Prerequisites: Python 3.11+ and a MongoDB 6.0+ instance (local Docker or a free
Atlas cluster).

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt