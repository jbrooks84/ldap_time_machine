# How it works

## A run, end to end

```
scheduler
   ↓
ltm.cli                              argument parsing, --dry-run setup
   |                                 (the ldap-time-machine console script;
   |                                 repo-root ldap_time_machine.py is a shim)
   ↓
ltm.pipeline.run()
   ├─ acquire lock                   no overlapping runs
   ├─ init database
   ├─ ldapsearch → LDIF → dicts      ltm.ldap_client, ltm.ldif_parser
   ├─ guardrail: result count sane?  abort rather than store partial data
   ├─ resolve baseline               yesterday, else the most recent snapshot
   ├─ diff, suppressing flaps        ltm.analysis
   ├─ write snapshot + changes       one BEGIN IMMEDIATE transaction
   ├─ confirm leavers, flag flappers
   └─ render, archive, send          ltm.report
```

## Modules

| Module | Responsibility |
|---|---|
| `ltm/_version.py` | The single source of the version number |
| `ltm/cli.py` | The console entry point: argument parsing, `--dry-run` setup |
| `ltm/config.py` | Layered configuration, flavor presets, role → attribute mapping |
| `ltm/records.py` | Role-based reads of a parsed record |
| `ltm/labels.py` | Reporting-window labels shared by logs, subjects, and headings |
| `ltm/secrets.py` | Credentials and SMTP settings |
| `ltm/logging_utils.py` | Rotating log setup |
| `ltm/ldap_client.py` | The one place a subprocess is run |
| `ltm/ldif_parser.py` | LDIF → dicts (Base64, line folding, paging artefacts) |
| `ltm/db.py` | Every SQL statement |
| `ltm/analysis.py` | Flap suppression, leaver confirmation, flapper detection |
| `ltm/dates.py` | generalizedTime parsing, tenure formatting |
| `ltm/highlights.py` | Optional highlight-tile data |
| `ltm/report.py` | HTML rendering and delivery |
| `ltm/pipeline.py` | Orchestration |

---

## Database

```sql
CREATE TABLE user_history (
  run_date  DATE,
  dn        TEXT,
  data      JSON,     -- the full record as fetched
  data_hash TEXT,
  PRIMARY KEY (run_date, dn)
);

CREATE TABLE changes (
  run_date  DATE,
  dn        TEXT,
  attribute TEXT,
  old_val   TEXT,
  new_val   TEXT
);

CREATE UNIQUE INDEX changes_unique
  ON changes (run_date, dn, attribute, old_val, new_val);
CREATE INDEX user_history_dn_run_date ON user_history (dn, run_date DESC);
CREATE INDEX changes_dn_attribute_run_date ON changes (dn, attribute, run_date);
```

Whole records are stored as JSON, one row per person per day. That is
deliberately redundant: adding an attribute to the config makes it available in
every *future* report with no migration, and the raw history stays queryable
without knowing anything about this codebase.

`changes` is derived — everything in it could be recomputed from
`user_history` — but keeping it materialised is what makes flap detection and
the change window cheap.

### Querying it directly

```bash
sqlite3 ldap_time_machine.db
```

```sql
-- Headcount over time
SELECT run_date, COUNT(*) FROM user_history GROUP BY run_date;

-- Everything ever recorded about one person
SELECT run_date, attribute, old_val, new_val
FROM changes
WHERE dn = 'CN=Jane Smith,OU=People,DC=example,DC=com'
ORDER BY run_date;

-- Who was in Security a year ago
SELECT json_extract(data, '$.displayName')
FROM user_history
WHERE run_date = '2025-07-26'
  AND json_extract(data, '$.department') = 'Security';

-- Everyone who changed department this month
SELECT run_date, dn, old_val, new_val
FROM changes
WHERE attribute = 'department' AND run_date >= '2026-07-01'
ORDER BY run_date;
```

A DN is not stable — moving between organisational units rewrites it — so join
on the username inside the JSON when tracking someone across a move.

---

## Design decisions worth knowing

### The guardrails fail safe

An empty or suspiciously small fetch aborts *before writing*. A `ldapsearch`
exit code of 4 (size limit exceeded) is refused outright rather than accepted
as partial data.

The asymmetry is deliberate: a missing day of history is recoverable by
re-running, but a day of *wrong* history becomes the baseline every later run
trusts, and it announces thousands of false departures tomorrow.

### Single-valued attributes are "last wins"

Paged results can repeat a DN across a page boundary. Accumulating those into
lists merges two people into one record — which still looks entirely plausible,
which is why it survives in production for a long time. Attributes that should
never be lists overwrite instead.

### Flap suppression excludes the current day

The lookback is `[today - N, today)`, exclusive at the top. Otherwise a re-run
would match the changes it just wrote and suppress them.

### Identity is the DN, and DNs are not stable

An OU move or rename rewrites a person's DN, and the directory reports that
as a delete plus a create — so the report shows a leaver and a joiner. This
is accepted rather than papered over: any heuristic that re-links renamed
entries (by username, by name) would sometimes be wrong, and a false "same
person" is worse than an honest pair of events.

### Leavers need continuous absence

One missing day is a directory glitch, not a resignation. Someone must be
absent from every run in the confirmation window. Leavers whose history already
contains a gap — they have disappeared and come back before — are marked with
an asterisk, because for those people "gone" has been wrong before.

### Report sections fail independently

Each section renders inside its own `try`/`except`. A broken section blanks
itself and logs; it does not abort the run. A report that is 90% right beats no
report at 06:10.

### Everything from the directory is escaped

Display names are attacker-influenced in more environments than people expect.
Every value reaching HTML goes through `html.escape`.

---

## Performance

### At scale

The optimisation pass that shaped this design was measured on a laptop
against a synthetic directory of **60,881 people with 500 days of history —
28.5 million snapshot rows in an 18 GiB database**:

| | Before | After |
|---|---|---|
| Full run | 76 s | **13.9 s** |
| Trend graph data | 53.4 s | 0.00 s |
| Health logging (×2) | 13.8 s | 0.00 s |
| Write 60,881 rows + indexes | 8.9 s | 8.9 s |
| Render report | 2.9 s | 2.9 s |

Two findings drove that, and neither was visible at small scale:

**The trend graph re-aggregated all of history, every run.** `GROUP BY run_date`
over 28.5M rows to produce 500 numbers — 53 seconds, growing without bound.
A `run_summary` table, written in the same transaction as each snapshot, makes
it O(days) instead of O(rows).

**The health log cost more than the work it described.** `COUNT(*)` and
`MIN`/`MAX(run_date)` over 28.5M rows, twice per run, was 13.8 s. The same
figures come out of `run_summary` for free.

There is a smaller lesson in how the health query was written. Its four
aggregates were originally one statement, which forces SQLite to serve them all
from a single index — it picked the one ordered by `dn`, so `run_date` values
arrived scattered and `COUNT(DISTINCT)` needed a temp B-tree over every row.
Split into separate statements, each got a suitable index and the whole thing
went from 102 ms to 2 ms on 800k rows. Combining aggregates is not free.

What remains is inherent: writing a day into a multi-GB database dirties index
pages across the whole file. A later re-measurement on a freshly built
database of the same shape put the whole steady-state run at 5–8 s — the
write dominates either way, and once a day it is a cheap price for a
queryable history.

### A line-level pass

Profiled with cProfile and line_profiler against 50,000 people (steady state,
93% CPU-bound): **3.05 s → 2.44 s**, and the first-ever run's report — where
every person is technically a joiner — from **8.9 MB of HTML to 124 KB**,
which matters because most mail relays refuse the former outright.

What the profile actually showed, in descending order of surprise:

- **One `strptime` line was the largest Python cost in the pipeline.**
  Ranking the seniority table parses a timestamp per person, and `strptime`
  re-parses its format string on every call (~6 µs). Building the `datetime`
  fields by hand after the existing regex validation is ~10× faster; 50,740
  calls dropped to 42.
- **The diff compared every key of every record, though ~99% of records are
  identical day to day.** Dict equality is a single C-level comparison and
  equal records cannot produce a diff, so a fast-path skip took the compare
  from 0.29 s to 0.06 s.
- **Today's aggregate counts re-read the database for records already in
  memory.** The report counted "now" by scanning the day's rows and
  `json_extract`-ing every blob — figures computable from the state the
  pipeline just wrote. One of four scans eliminated.
- **The LDIF parser paid a list scan and a function call per attribute
  line.** Membership tests against the config lists (660,000 O(13) scans)
  became frozensets, and the single-value "last wins" branch is inlined at
  the hottest call site in the program.
- **Stored JSON carried whitespace.** Compact separators shrink every row —
  and therefore every future read and backup — by about 6%.

What remains at 50,000 people is ~0.8 s of SQLite C, ~0.6 s of LDIF text
processing, and ~0.2 s of JSON codec — native code and I/O, which is where a
profile should end.

### At everyday scale

Against ~2,300 people with a full year of history — 800,000 snapshot rows in a
582 MiB database:

| Step | Time |
|---|---|
| Open database, log health | 0.02 s |
| Fetch and parse 2,288 LDIF records | 0.05 s |
| Diff against the previous snapshot | 0.02 s |
| Write snapshot + change rows | 0.12 s |
| Confirmed leavers, flappers, change window | 0.03 s |
| Render report (6 breakdowns, trend graph, 100-row seniority) | 0.17 s |
| **Full run** | **0.72 s** |

The shapes below are why, and they matter more than any pragma. Following
[measured SQLite guidance](https://www.sqlite.org/):

**DN-set comparisons never read the table.** The leaver window compares seven
days of *key sets*. Reading only `dn` is answered entirely from the
`(run_date, dn)` primary-key index — a test asserts `COVERING INDEX` appears in
the query plan. Loading full snapshots to compute a set difference would
deserialise a week of JSON for nothing.

**Aggregate counts make one pass per date.** Six breakdown tables across four
dates is 24 attribute lookups; done naively that is 24 scans re-parsing the same
JSON documents. Extracting every attribute in a single pass makes it 4.

**Flap detection preloads.** Asking the database "have we seen this value
recently?" once per changed attribute is thousands of queries on a
reorganisation day, all against the same small slice of one table. That slice is
loaded once and answered from memory — which also makes the matching logic a
pure function over a dict, and therefore testable.

**Batched lookups.** Flapper detection and record hydration chunk their DNs
into single queries instead of looping one query per person.

**Writes take the lock up front.** A plain `BEGIN` starts as a read transaction,
and the upgrade to a write lock *cannot* wait on `busy_timeout` — it fails
instantly with `SQLITE_BUSY`. All writes use `BEGIN IMMEDIATE`.

**Connection settings.** WAL with `synchronous=NORMAL`; `mmap_size` at 256 MiB
(the single biggest read lever); `temp_store=MEMORY` for the aggregate sorts;
`journal_size_limit` so the WAL cannot grow unbounded between runs.

**`--dry-run` snapshots with `VACUUM INTO`**, not a file copy — see
[operations.md](operations.md#backups) for why that distinction matters.
