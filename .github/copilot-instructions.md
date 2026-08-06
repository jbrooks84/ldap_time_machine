# Copilot instructions

A condensed copy of [AGENTS.md](../AGENTS.md), which is the canonical
statement of this project's standards. This file is self-contained because
Copilot code review does not follow imports. When the rules change, change
them in `AGENTS.md` first, then update this summary.

LDAP Time Machine snapshots an LDAP directory daily into SQLite, diffs each
snapshot against the last, and emails a report. It runs unattended against
production directories, so wrong output is worse than no output.

## Gates

```bash
pytest                  # coverage gate fails the run below 100%
ruff check .
ruff format --check .   # a separate gate from lint
```

## Hard rules — merge blockers

1. **Never hardcode a directory attribute name outside a flavor preset.** The
   code works in terms of roles. Add the role to `ATTRIBUTE_ROLES` in
   `ltm/config.py`, map it in every entry of `FLAVORS`, and read it with
   `records.value(record, "your_role")`. A literal `sAMAccountName` in a
   module breaks every OpenLDAP deployment while looking fine on AD.
2. **Coverage stays at 100%, line and branch.** New code arrives with tests.
3. **Never loosen a gate to make a change pass.** No lowering `fail_under`, no
   new ruff `ignore` entries, no `# noqa`, no `pragma: no cover`, no narrowed
   `testpaths`, no deleted assertions. Weakening the gate is a larger
   violation than the failure it hides. Unreachable branch → delete it.
4. **Guardrails fail safe.** A small or empty fetch aborts before writing. A
   day of wrong history is worse than a missing day: every later run trusts it.
5. **Everything from the directory is escaped** before it reaches HTML.
6. **No new heuristic without a config knob.**
7. **Nothing identifying in the tree** — no real employer, person, hostname or
   internal detail anywhere, including commit messages. Enforced by
   `tests/test_edge_cases.py`; extend it, never weaken it.
8. **The version lives only in `ltm/_version.py`.**

## Invariants

Single-value attributes are "last wins" (paged results repeat DNs across page
boundaries, and merging them fuses two people into one plausible-looking
record). Flap suppression excludes the current day, or a re-run suppresses
what it just wrote. Leavers need continuous absence — one missing day is a
glitch. Report sections fail independently rather than aborting the run.

## Tests

Extend the sample history in `tests/conftest.py` rather than writing fresh
fixtures; it already holds a joiner, confirmed leavers, a one-day absence that
must not count as a departure, a gap in someone's history, a homonym pair, and
a bulk rename. The suite uses real subprocesses, a real SQLite database and
real rendering — only SMTP is stubbed.

## Comments

The code says what it does. Comments are for the reasoning that is not visible
from the code: the bug a check prevents, why a default is what it is, the
failure mode being defended against.
