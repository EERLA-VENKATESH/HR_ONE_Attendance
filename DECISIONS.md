# DECISIONS.md

**1. Indexes.** Six, created at startup via `ensure_indexes()`:
- `employees.emp_code` (unique) - uniqueness on create.
- `employees.(department, joined_on)` - headcount + department filter.
- `attendance_logs.(emp_code, date)` (unique) - one record per employee per day; punch-in race-safety.
- `attendance_logs.(date desc, emp_code asc)` - `/attendance` list sort.
- `attendance_logs.(emp_code, date desc)` - per-employee history and monthly summary.
- `attendance_logs.(date, status)` - trend and status filters.

Considered a single-field index on `status` and rejected it: five values means poor selectivity.

**2. Punch-in race.** I rely on the unique `(emp_code, date)` index. Two concurrent requests both compute the same `date`, both attempt `insert_one`, the first commits, the second raises `DuplicateKeyError`, which I catch and translate to 409. No check-then-insert.

**3. Ties at the cutoff.** The leaderboard uses `$setWindowFields` with `$rank`, applied *before* filtering by `limit`. Two employees tied at rank 10 with `limit=10` both have rank 10, both returned - the response may exceed `limit`. Rows sorted by `total_late_minutes desc, emp_code asc`.

**4. Headcount with zero logs.** The department summary starts from `employees` (filtered by `joined_on <= end of month`), then `$lookup`s logs and `$reduce`s them. Employees with zero logs contribute zero to metric sums but still count in `headcount`. Late joiners are excluded by the `$match`.

**5. 100x data.** Add a per-month partial index for analytics, pre-aggregate monthly summaries nightly, use Atlas time-series collections for `attendance_logs`, and enable the Atlas query profiler to catch regressions.