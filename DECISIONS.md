# DECISIONS.md

1. **Indexes.** `attendance_logs`:
   - unique `(emp_code, date)`: one record per day, punch-in races, monthly ranges and the `$lookup` joins.
   - `(date desc, emp_code)`: the list's exact sort order and month-wide leaderboard scans.
   - `(status, date desc, emp_code)`: status-filtered lists and their counts.
   - `(emp_code, punch_in desc)`: punch-out's "latest punch-in".

   `employees`:
   - unique `emp_code`
   - `(department, emp_code)`
   - `joined_on`, for the headcount filter.

   I rejected a partial index on `late_minutes > 0`: a small leaderboard gain for a cost on every write.

2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one` without reading first. The
   unique index lets exactly one insert succeed. The other gets `DuplicateKeyError`, which I return as 409.

3. **Ties.** `$rank` over total late minutes gives 1, 2, 2, 4. I then keep every row with `rank <= limit`, so a tie at
   the cutoff is returned in full, even beyond `limit`. Tied rows are ordered by `emp_code`.

4. **Headcount.** The pipeline starts from `employees` (`joined_on` <= month end), left-joins that month's logs with
   `$lookup`, and unwinds with `preserveNullAndEmptyArrays`. Every employee adds 1 to headcount; no logs simply adds
   zeros elsewhere.

5. **At 100x.** The department summary runs one `$lookup` per employee (about 250 ms for 500 employees and 100k logs
   locally). I would maintain per-employee monthly rollups with `$merge` on write and read those instead.

**Interpretations** (details in README): a trend range over 92 days, counted inclusively, is 422, so a full quarter is
accepted. The 7-day moving average averages the daily rates after they are rounded to 4 places.
