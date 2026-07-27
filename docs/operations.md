# Running it in production

## Scheduling

Pick a time when the directory is quiet. A run takes seconds.

### cron

```cron
10 6 * * * cd /opt/ldap_time_machine && .venv/bin/python ldap_time_machine.py
```

### systemd timer

Preferred — failures land in the journal instead of a mail nobody reads.

```ini
# /etc/systemd/system/ldap-time-machine.service
[Unit]
Description=LDAP Time Machine daily snapshot

[Service]
Type=oneshot
User=ldaptm
WorkingDirectory=/opt/ldap_time_machine
ExecStart=/opt/ldap_time_machine/.venv/bin/python ldap_time_machine.py
# 75 = EX_TEMPFAIL: another run already holds the lock. Benign, not a failure.
SuccessExitStatus=75
```

```ini
# /etc/systemd/system/ldap-time-machine.timer
[Unit]
Description=Run LDAP Time Machine daily

[Timer]
OnCalendar=*-*-* 06:10:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now ldap-time-machine.timer
systemctl list-timers ldap-time-machine.timer
```

`Persistent=true` catches up a run missed while the machine was off.

### Overlapping runs

Prevented by a lock file regardless of scheduler. A second run exits with
status 75 — the sysexits `EX_TEMPFAIL` convention for "temporary, retry
later". The service file above declares 75 a success so an overlap never
shows up as a failed unit; cron does not act on exit codes, so nothing extra
is needed there.

### Before any change reaches the schedule

```bash
.venv/bin/python ldap_time_machine.py --dry-run
```

A dry run does everything a real run does — including the actual directory
query — except that it writes to a throwaway copy of the database and sends no
mail. The rendered report lands in `email_archive/dryrun_<timestamp>.html`.

Dry-run reports are retained separately from real ones, so testing a change can
never evict the production reports you might want to compare against.

---

## Monitoring

Every run logs a health line:

```
Database health: size=0.57 GiB rows=800298 changes=2745 run_dates=365
range=2025-07-27..2026-07-26 freelist_pages=0 wal=0.0 MiB
estimated_growth=1.6 MiB/day disk_free=130.7 GiB runway=83707 days
```

A healthy run ends with `Run completed successfully`.

Worth alerting on:

| Condition | Why |
|---|---|
| No `Run completed successfully` line for a day | The job did not run, or died. A job that never runs reports nothing — silence is not success |
| `WARNING` containing `checkpoints are not completing` | The WAL is growing unbounded; usually a long-lived reader holding it open |
| `WARNING` containing `schedule a VACUUM` | The free list is over 20% of the file |
| Falling `runway` | Disk will run out at the current growth rate |

---

## Troubleshooting

Start with `ldap_time_machine.log`.

| Log message | Cause | What happened to the data |
|---|---|---|
| `LDAP credentials unavailable` | Credentials file missing, unreadable, or incomplete | Nothing written |
| `ldapsearch failed (49)` | Bad bind credentials, or the account is locked | Nothing written |
| `ldapsearch timed out` | Directory slow or unreachable; raise `ldap.timeout_seconds` | Nothing written |
| `ldapsearch size limit exceeded` | The server truncated results. Raise its limit or narrow the filter | Nothing written — partial data is refused deliberately |
| `below 90% of the previous snapshot` | The guardrail tripped | Nothing written. Investigate, then re-run |
| `Another run is already active` | A run is genuinely in progress. The lock is a kernel `flock`, released automatically even if a run crashes, so it cannot go stale | Nothing written. The lock file holds the running PID — wait for that process or investigate it. Do not delete the file: a fresh run would then lock a new inode and could overlap the old one |
| `Email send failed` | SMTP unreachable or rejected | Everything written, report archived — only delivery failed |
| `Failed to render <section>` | A bug in one report section | Everything written; the other sections rendered |

### Replaying a past report

```bash
.venv/bin/python ldap_time_machine.py --replay 2026-03-14
```

Re-renders the report that run date produced, from stored history alone: the
same joiner and change windows, leaver confirmation, tenure and trend graph
as of that date. Nothing is fetched, nothing is emailed, and no snapshot is
written — the output lands in the dry-run archive bucket (`dryrun_*.html`).
Use it to answer "why did that report say X?" long after the email is gone,
or to see how a retuned noise control would have rendered a past day.

The date must be one a run actually recorded; the error message names the
latest date on file if you miss.

### The report looks wrong

Open the archived HTML from `email_archive/` directly in a browser. If that
looks right, the mail client mangled it — usually Outlook discarding the
`<style>` block. If a section is missing there too,
`grep "Failed to render" ldap_time_machine.log`; sections fail independently by
design so one bug cannot blank the whole report.

### Everyone shows as a leaver

Almost always a filter or bind change altering which entries are returned, not
a real event. The result-count guardrail should have caught it — check whether
it was lowered or disabled. The previous snapshot is intact either way; fix the
filter and re-run.

### One person appears as both a leaver and a joiner

Their DN changed — an OU move or a rename rewrites it, and identity is keyed
by DN. The directory genuinely deleted one entry and created another, so the
report reflects that; the person's history continues under the new DN. This
is a known limitation, not a detection bug.

### A quiet report on a day you expected changes

Check `Suppressed flaps: N` in the log. If the change oscillated recently it
was suppressed on purpose — see `flap_lookback_days` in
[configuration.md](configuration.md). The change is still in the `changes`
table; only the report omitted it.

---

## Maintenance

The database only grows. Deleting rows returns space to SQLite's free list, not
to the filesystem — reclaiming it needs a rewrite:

```bash
sqlite3 ldap_time_machine.db "VACUUM;"
```

Run it off-peak: it takes an exclusive lock and needs roughly the database's own
size again in free space.

### Backups

Back up with `VACUUM INTO`, never `cp`. Copying a live SQLite file can capture
a torn page mid-write, and the WAL holds committed data the main file does not
have yet:

```bash
sqlite3 ldap_time_machine.db "VACUUM INTO '/backup/ltm-$(date +%F).db';"
```

This is safe while the database is in use, and the output is already compacted.

Then **test a restore**. An untested backup is a hope, not a backup. And alert
on backup *freshness* — a backup job that silently stopped running reports
nothing at all.

### Rotating the bind password

Update `LDAP_PASSWORD` in the credentials file and verify before the next
scheduled run:

```bash
.venv/bin/python ldap_time_machine.py --dry-run 2>&1 | grep -E "fetch completed|failed"
```

### On a large directory

Measured on ~61,000 people with 500 days of history (28.5M rows, 18 GiB): a
steady-state run takes 5–8 seconds on a laptop SSD, and has measured as high
as ~14 depending on filesystem state. Most of it is writing the day's
snapshot and updating indexes, which is inherent — a day's rows dirty index
pages scattered across the whole file.

Two things to expect at that size:

* **The WAL grows during the write**, to a few hundred MB. Each run truncates
  it afterwards, so it should be near zero between runs. If it is not, a
  long-lived reader is pinning it.
* **The first run after upgrading** from a version without `run_summary` pays a
  one-time full scan to backfill it — about a minute at 28M rows. It is logged,
  and it happens once.

### Growth

Roughly `record count × 700 bytes` per day including indexes — measured at
670–760 bytes per row across synthetic directories from 2,300 to 61,000
people. That is about 3.5 MiB/day for 5,000 people, or 1.2 GiB/year; the
health line's `estimated_growth` reports the measured rate for yours. There
is no automatic pruning, deliberately: the full history is what makes the
trend deltas and seniority table work. If disk genuinely becomes a problem,
move old `user_history` rows to a cold database rather than deleting them.
