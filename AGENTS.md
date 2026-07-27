# AGENTS.md

Guidance for AI agents working in this repository.

Deeper background lives in [docs/architecture.md](docs/architecture.md) (how a
run works, schema, design decisions) and
[docs/configuration.md](docs/configuration.md) (roles, flavors, tuning). Read
the architecture page before changing anything in `ltm/`.

## What this is

LDAP Time Machine snapshots an LDAP directory daily into SQLite, diffs each
snapshot against the last, and emails a report. It runs unattended against
production directories, so wrong output is worse than no output.

## Entry points

- `ldap_time_machine.py` → `ltm.pipeline.run()`
- `ldap_time_machine.py --dry-run` → throwaway database copy, no mail sent
- `tools/*.py` → standalone utilities, not part of the scheduled run

## Layout

```
ltm/config.py         Layered config, flavor presets, role→attribute mapping
ltm/records.py        Role-based reads of a parsed record
ltm/secrets.py        Credentials and SMTP settings
ltm/logging_utils.py  Rotating log setup
ltm/ldap_client.py    The only place a subprocess runs
ltm/ldif_parser.py    LDIF → dicts
ltm/db.py             Every SQL statement
ltm/analysis.py       Flap suppression, leaver confirmation, flapper detection
ltm/dates.py          generalizedTime parsing, tenure formatting
ltm/highlights.py     Optional highlight-tile data
ltm/report.py         HTML rendering and delivery
ltm/pipeline.py       Orchestration
```

## The one rule that matters most

**Never hardcode a directory attribute name outside a flavor preset.**

The code works in terms of *roles* (`username`, `display_name`, `country`).
`ltm/config.py` maps each role to whatever the configured directory calls it,
and `ltm/records.py` reads through that map. A literal `sAMAccountName` in a
module silently breaks every OpenLDAP deployment while looking perfectly fine
on Active Directory.

To add an attribute: add the role to `ATTRIBUTE_ROLES`, map it in every entry
of `FLAVORS`, then read it via `records.value(record, "your_role")`.

## Invariants worth understanding before changing anything

- **Guardrails fail safe.** An empty or suspiciously small fetch aborts before
  writing. A day of *wrong* history is worse than a missing day, because every
  later run treats it as the baseline.
- **Single-value attributes are "last wins."** Paged results can repeat a DN
  across a page boundary; accumulating those into lists merges two people into
  one record that still looks plausible.
- **Flap suppression excludes the current day.** Otherwise a re-run suppresses
  the changes it just wrote.
- **Leavers need continuous absence.** One missing day is a directory glitch,
  not a resignation.
- **Report sections fail independently.** A broken section blanks itself and
  logs; it does not abort the run.
- **Everything from the directory is escaped** before it reaches HTML.

## Noise controls are configuration, not policy

`leaver_confirm_days`, `flap_lookback_days`, `bulk_rename_min`,
`min_ldap_result_ratio`, and `unknown_ratio_limit` exist because directories
lie temporarily. Their defaults suit a large, messy enterprise directory. Every
one can be disabled — see the table in the README. Do not treat any of them as
a fixed assumption, and do not add a new heuristic without a config knob.

## Before you finish

```bash
pytest              # must pass; coverage must stay at 100%
ruff check .
ruff format .
```

Coverage is gated at 100% in `pyproject.toml`. If a branch is genuinely
unreachable, delete it rather than exempting it.

Prefer extending the sample history in `tests/conftest.py` over writing fresh
fixtures — it already contains a joiner, confirmed leavers, a one-day absence
that must not count as a departure, a person with a gap in their history, a
homonym pair, and a bulk rename.

## Generated at runtime (not in git)

`ldap_time_machine.db`, `ldap_time_machine.log`, `ldap_time_machine.lock`,
`trend_graph.png`, `email_archive/`, `config.yml`, `credentials.yml`
