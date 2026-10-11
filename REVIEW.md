# REVIEW.md

Defects found in the starter `app/main.py` and how they were fixed.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Floors minutes *before* comparing to the 10-minute grace, so 09:40:01 returns 0 instead of 10. R2 says the comparison must be on raw seconds. | Punch in at 09:40:01 for a 09:30 shift -> returns 0, should be 10. | Compare `seconds <= 600` on raw elapsed seconds, then floor for the answer. |
| 2 | `compute_late_minutes` | Timezone: `punch_in.replace(hour=...)` uses whatever tz `punch_in` carries. PyMongo returns naive UTC, so shift start is built in UTC, not IST. | Punch in at 09:35 IST (04:05 UTC) -> computes -5h25m, returns 0, should be 5. | Added `to_ist()` helper; convert `punch_in` to IST before subtracting. |
| 3 | `compute_work_hours` | Uses Python `round()`, which is banker's rounding. R4 requires half-up. | `round(2.675, 2)` -> 2.67, should be 2.68. | Use `Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP)`. |
| 4 | `compute_overtime` | Missing the >=30-minute threshold (R3); overtime under 30 should be 0. | Punch out 20 min after shift end -> returns 20, should be 0. | Added `if minutes >= 30` gate. |
| 5 | `compute_overtime` | Wrong date for overnight shifts: uses `date_str` as-is. | Overnight 22:00-06:00 -> overtime computed against same-day 06:00, wildly wrong. | Detect `shift_end <= shift_start`, add a day. Signature now takes `shift_start`. |
| 6 | `POST /employees` | Check-then-insert race: two concurrent creates can both pass the check. | Two simultaneous creates of same `emp_code` -> both 201. | Unique index on `emp_code`; catch `DuplicateKeyError` -> 409. |
| 7 | `POST /employees` | `created_at = datetime.now()` - naive local time, not UTC. | Response drifts by local tz offset. | `datetime.now(timezone.utc)` stored naive, serialize to epoch millis. |
| 8 | `GET /employees` | Pagination off-by-one: `skip = page * page_size` skips an entire first page. | `?page=1` returns records 21-40, not 1-20. | `skip = (page - 1) * page_size`. |
| 9 | `GET /employees` | `total` ignores the filter (`count_documents({})`). | `?department=X` reports total = all employees, not just X. | `count_documents(q)`. |
| 10 | `POST /attendance/punch-in` | Racy check-then-insert; no R1 overnight date logic; leaks an extra `id` field. | Two concurrent punch-ins for same employee/date both succeed. | Compound unique index `(emp_code, date)`; catch `DuplicateKeyError`. Added `compute_attendance_date` for R1. Removed `id`. |
| 11 | `GET /attendance` | Loads entire collection into memory, sorts and slices in Python. | Sluggish on 100k records. | `find(q).sort([("date", -1), ("emp_code", 1)]).skip().limit()` - pushed to MongoDB. |
| 12 | `GET /health` | Always returns 200, even when MongoDB is down. | Stop MongoDB -> `/health` still says ok. | Ping admin database; 503 on failure. |

## Not defects (checked and left as-is)

- `load_dotenv()` before reading env vars is correct; python-dotenv doesn't override real env vars.
- `emp_code` never being the `_id` is enforced correctly - the starter never sets `_id` on inserts.
- Pydantic model shapes for `EmployeeIn` / `PunchInIn` are correct; only validation constraints were missing (added).