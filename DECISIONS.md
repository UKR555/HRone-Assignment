# DECISIONS.md

1. **Indexes.**
   - `employees`: Compound unique index on `emp_code` (enforces customer business key uniqueness and prevents race conditions on creation); compound index `(department, emp_code)` (accelerates department filtering and sorting); index `(joined_on, department)` (optimizes headcount queries).
   - `attendance_logs`: Compound unique index `(emp_code, date)` (guarantees at most one attendance record per employee per day and prevents concurrent punch-in races); compound index `(emp_code, punch_in)` (enables fast lookup of latest punch-in for punch-out); compound index `(date, emp_code)` (supports fast list sorting and date filtering); compound index `(date, late_minutes)` (accelerates late leaderboard pipeline filtering).
   - *Rejected*: Indexing nested `history.at` or `history.changes` fields was considered and rejected, as history entries are strictly read as part of full attendance documents and never queried directly across documents.

2. **Punch-in race.**
   - When two identical punch-in requests arrive at the same instant, both compute the IST attendance date.
   - Both attempt `db.attendance_logs.insert_one(doc)`.
   - The unique compound index on `(emp_code, date)` ensures MongoDB atomically allows only one document insertion.
   - The winning request succeeds and returns HTTP 201.
   - The losing request catches `pymongo.errors.DuplicateKeyError` and raises HTTP 409 (`Already punched in for this date`).

3. **Ties.**
   - The late leaderboard uses MongoDB's `$setWindowFields` with `$rank` (standard competition 1224 ranking), where tied employees receive the same rank and subsequent ranks are skipped (e.g., 1, 2, 2, 4).
   - The filter `{"rank": {"$lte": limit}}` applies after ranking.
   - If employees tie at rank `<= limit`, all tied employees are returned, resulting in more than `limit` rows. Rows are sorted by `total_late_minutes` descending, then `emp_code` ascending.

4. **Headcount.**
   - The department summary aggregation pipeline initiates on the `employees` collection (not `attendance_logs`), filtering by `joined_on <= month_end` and optional department filter.
   - It performs a `$lookup` to fetch matching `attendance_logs` for that month.
   - Employees without any attendance records remain in the pipeline stream and count towards department headcount (`$sum: 1`), ensuring accurate headcount reporting.

5. **One thing you would change at 100x data.**
   - At 100x scale (10+ million logs), multi-stage aggregations across large collections during peak user traffic would incur significant CPU and memory overhead.
   - I would introduce incremental pre-aggregation using MongoDB Change Streams or background queue workers to maintain daily/monthly summary collections. Analytics endpoints would query pre-computed stats in O(1) time instead of running full-table pipeline scans.
