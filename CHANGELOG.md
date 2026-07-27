# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] — 2026-07-27

First public release. Generalised from an internal Active Directory reporting
tool into something any LDAP directory can run, then hardened against
large-scale synthetic testing — up to ~61,000 people with 500 days of history
(28.5 million rows, 17.8 GiB) — before launch.

The internal tool this grew out of carried its own version numbers; public
versioning starts fresh here at 1.0.0. The internal feature history, mapped
onto 0.x numbers, follows this entry.

### Added

- **Directory flavors.** `active_directory`, `openldap`, and `generic`
  (RFC 4519) presets, each supplying a sensible default search filter and
  attribute map.
- **Attribute roles.** The code works in terms of roles (`username`,
  `display_name`, `country`) that a flavor maps to whatever the directory
  actually calls them, so the same report renders against Active Directory and
  OpenLDAP unchanged. Individual roles can be remapped or unmapped; an
  unmapped role is not fetched, its report sections are skipped, and a tracked
  role the flavor leaves unmapped is named in one warning at run start.
- **Layered configuration.** Built-in defaults → flavor preset → `config.yml` →
  `LTM_*` environment variables, including overrides for the noise controls
  (`LTM_LEAVER_CONFIRM_DAYS`, `LTM_FLAP_LOOKBACK_DAYS`,
  `LTM_MIN_LDAP_RESULT_RATIO`, `LTM_MAX_TABLE_ROWS`) for containers and CI.
  The package imports and the test suite runs on a fresh checkout with no
  configuration at all.
- **Configurable noise controls.** Leaver confirmation, flap suppression,
  bulk-rename rollup, the result-count guardrail, and unknown-value
  suppression are all settings, each individually disableable. None of them
  affect what is stored — only what is reported — so retuning never costs
  history.
- **`report.max_table_rows`** (default 500): a hard cap on rows in any single
  people-table. The first run against a large directory reports every person
  as a joiner — measured at 50,000 people, an uncapped first report was 9 MB
  of HTML, larger than most mail relays accept. Homonyms sort to the top, so
  the cap never hides the disambiguated rows.
- **Optional SMTP authentication.** STARTTLS and username/password, alongside
  unauthenticated relay support.
- **`--replay YYYY-MM-DD`** — re-render the report any past run date
  produced, from stored history alone: the same windows, leaver
  confirmation, tenure and trend graph as of that date. Never fetches,
  writes no snapshot, never emails; archives to the dry-run bucket.
- **Installed-package data paths.** `paths.base_dir` defaults to the repo
  root on a checkout but to `$XDG_DATA_HOME/ldap-time-machine` when
  installed as a wheel, so the database and logs never silently land inside
  `site-packages`.
- **`--version`**, and `ldap.start_tls`, `ldap.page_size`, `ldap.extra_args`
  for directories that need them.
- **Configurable report presentation** — organisation name, member label,
  subject prefix, footer link, per-table row caps, and which roles get their
  own change section.
- **Documentation** — README, configuration, operations, and architecture
  guides.
- **CI** across Python 3.9–3.13, with a package build job.

### Changed

- **Date parsing accepts every LDAP generalizedTime variant** — Active
  Directory's `.0Z`, OpenLDAP's bare `Z`, and numeric offsets. Previously only
  the AD form parsed, which meant any other directory reported "Unknown"
  tenure for everyone.
- **The highlights strip is a configuration flag** rather than a commented-out
  line of code.
- **A successful `--dry-run` deletes its database copy**; a failed one keeps
  it for post-mortem. The copy is live-database sized.
- **The LDIF parser emits one summary WARNING per run** when Base64 values
  fail to decode or when records parse with none of the requested attributes —
  the all-records-empty case is exactly what a misconfigured attribute map
  looks like. Per-value detail stays at DEBUG.
- **`counts_from_records` refuses multi-valued attributes** with a
  `ValueError` rather than silently counting different keys than the SQL path.
- **Aggregate tables break count ties alphabetically**, so two reports over
  the same data always diff clean.
- **Durations under a second are logged in milliseconds** — `%.1fs` rendered a
  48 ms step as `0.0s`, which reads like it never ran.
- **The trend graph's y-axis padding scales with the data**, and report date
  formatting avoids `%-d`, a glibc/BSD extension that raises on Windows.
- **A DN rename (OU move) reports as a leaver plus a joiner**, documented in
  operations and architecture along with why re-linking heuristics are
  deliberately not attempted.
- **The joiners section honours `windows.window_days`.** Its heading always
  promised a window; it now compares against the pre-window baseline instead
  of always diffing the latest two runs. At the default 1-day window the
  behaviour is unchanged.
- **Flapper detection counts missed runs, not calendar gaps.** On a Mon–Fri
  schedule every weekend used to read as a gap, eventually marking everyone
  a flapper; now only absence from a run that actually happened counts.
- **The standalone lookup tools open the database read-only**, so an ad-hoc
  query can never lock or modify the file the nightly run writes.
- **`SEND_EMAIL: false` disables delivery quietly.** The master off-switch
  previously still logged an ERROR about missing SMTP fields — an email-less
  deployment is a documented configuration, not a misconfiguration.

### Performance

Measured on a laptop: 0.7 s against 2,300 people with a year of history;
5–14 s steady state against ~61,000 people with 500 days (28.5M rows,
17.8 GiB), dominated by writing the day's snapshot. Following measured
SQLite guidance:

- **A maintained `run_summary` table** (one row per day, written in the same
  transaction as the snapshot, backfilled automatically when absent) keeps
  the trend graph and the health metrics O(days) instead of O(all rows ever
  stored).
- **Leaver windows compare DN sets read from the primary-key index** rather
  than deserialising a week of JSON snapshots; a test asserts the covering
  index is used.
- **Aggregate counts extract every attribute in one pass per date**, and
  today's counts come straight from the records already in memory.
- **Flap detection preloads its lookback window once** instead of querying per
  changed attribute.
- **The snapshot diff fast-paths unchanged records** — dict equality is one
  C-level comparison, and on a typical day that skips ~99% of the directory.
- **generalizedTime parsing builds datetime fields directly** after regex
  validation instead of calling `strptime` (~10× faster; the seniority
  ranking calls it once per person).
- **The LDIF parser uses frozenset membership** with the single-value
  "last wins" branch inlined at its hottest call site.
- **Writes use `BEGIN IMMEDIATE`**; connections set `mmap_size`,
  `temp_store`, and `journal_size_limit`; the WAL is truncated at the end of
  every run, and health is logged after truncation so the thresholds measure
  the between-runs state.
- **Snapshots are stored as compact JSON** (~6% smaller rows) and stream into
  `executemany` through a generator; `--dry-run` snapshots via `VACUUM INTO`
  rather than copying a live database file; JSON paths are bound parameters.

### Fixed (relative to the internal predecessor)

- Database health logging reports free space for the database actually in
  use — during a dry run, the copy's filesystem rather than the live one.
- `get_attribute_counts` is fully guarded against database errors, so a
  transient failure blanks a report column instead of aborting a run that
  has already written its snapshot.

### Security

- The bind password is passed to `ldapsearch` via a 0600 temporary file
  rather than the command line, where it would be visible in process
  listings.
- Every value originating from the directory is HTML-escaped before reaching
  the report.
- Database and log files are created mode 0600.
- Credential and config files are gitignored by name; the `*.example.yml`
  templates are tracked.

### Operational notes

- Overlapping runs exit with status 75, the sysexits `EX_TEMPFAIL` convention
  for "temporary, retry later"; the documented systemd unit declares it a
  success so an overlap never shows up as a failed unit.
- Runs warn when the between-runs WAL exceeds 100 MiB or the free list
  exceeds 20% of the file (a VACUUM is due).

---

## Pre-1.0 history

## 0.4.9 — ~2026-05 (internal v4.9)

The final internal release, aimed at keeping the report readable at scale.

### Added

- Homonym disambiguation: amber row highlight, username badge, sort-to-top,
  and an explanatory footnote — so two people sharing a display name can
  never be read as one.
- The single "Attribute Changes" list split into typed sections (title,
  department, manager, location), with mass department renames rolled up
  into one row apiece so individual moves stay visible.
- The four-tile highlights strip (longest-tenured leaver, country mover,
  office mover, net headcount) with per-tile failure isolation; validated
  against historical data and shipped built but disabled — 1.0.0 later made
  it a configuration flag.
- The `--dry-run` verification harness: copy the database, redirect all
  writes, skip SMTP, archive under a separate retention bucket.

### Changed

- Trend graph moved to auto-scaling date axes so long histories stay
  readable.

Deferred at the time, with reasons recorded: a leaver-tenure histogram,
keyword-based promotion detection, a same-day "pending leaver" callout (the
confirmation gate was deliberate), a manager-churn tile, and historical
report replay.

## 0.4.8 — ~2026-04 (internal v4.8)

- Leaver confirmation formalised as a continuous-absence window, recorded
  in its own commit as a deliberate decision — the gate that stops a
  directory glitch from reporting mass resignations.
- "Known flapper" flagging: confirmed leavers whose history already shows a
  presence gap are marked, because for those people "gone" has been wrong
  before.
- Logging depth improved; a rotation fix in the same effort did not
  initially rotate, and was corrected in a later change.

## 0.4.4 — ~2026-Q1 (internal v4.4, documentation snapshot)

The earliest internal version whose feature set is directly documented:
daily snapshots into the two-table SQLite schema, the HTML email with an
embedded trend graph, attribute-change tracking for titles, departments,
managers and locations, multi-window trend tables, division and
business-category breakdowns, A→B→A flap suppression with same-day re-runs
excluded, LDIF hardening (Base64 DNs, line folding, search-metadata
filtering), and refusal of size-limit-truncated results. Traces in the
ignore rules suggest a standalone per-country graph existed somewhere in
this era and was dropped in favour of the aggregate tables.

## 0.2.1 — ~2025 (internal tooling era)

The three companion CLIs date from before the modular package: a
current-record lookup, a per-person change tracer that already understood
DN migrations (the internal source's docstring was marked "v2.1", which is
where this entry's number comes from), and a live directory explorer for
users, groups, membership, and demographics. All three were carried into
1.0.0, rewritten against the configuration layer.

## 0.1.0 — ~2024 (prototype)

A monolithic script, the first HTML email, and the two-table schema —
full-record JSON snapshots plus an attribute-change log — that survives
unchanged into 1.0.0. The era's scar: the "Siamese twin" bug, where paged
LDAP results repeating a DN across a page boundary merged two people into
one plausible-looking record. The single-value "last wins" rule that
prevents it dates from this era and still stands guard in the parser.

[Unreleased]: https://github.com/jbrooks84/ldap_time_machine/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/jbrooks84/ldap_time_machine/releases/tag/v1.0.0
