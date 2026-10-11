# Employee Attendance & Analytics API

FastAPI + MongoDB service for employee attendance with analytics.

## Run

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

Create a `.env` file in the repo root:

MONGO_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/
MONGO_DB=attendance_db

Load sample data (optional) and start the server:

python sample_seed.py
uvicorn app.main:app --port 8000

API docs: http://localhost:8000/docs

## What's implemented

- All endpoints in openapi.yaml.
- Indexes created idempotently at startup.
- Business rules R1-R10 (timezone, overnight shifts, half-day, rounding, ties, headcount).
- Analytics as MongoDB aggregation pipelines.
- Race-safe punch-in/punch-out via unique indexes and atomic find_one_and_update.
- Optimistic concurrency on PATCH via history-length check.

## Notes

- All application code lives in app/main.py.
- See REVIEW.md for starter bugs found and fixed, DECISIONS.md for design notes.