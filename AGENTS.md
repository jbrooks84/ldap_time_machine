# AGENTS.md

Guidance for AI agents — and a quick reference for humans — working in this
repository. This file is an index, not a manual: read the rules here, then
follow the task map to the one document that covers what you are doing.

Human contributors should also read [CONTRIBUTING.md](CONTRIBUTING.md).

## What this is

LDAP Time Machine snapshots an LDAP directory daily into SQLite, diffs each
snapshot against the last, and emails a report. It runs unattended against
production directories, so wrong output is worse than no output.

## Setup and commands

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pre-commit install     # runs the gates below on every commit
```

```bash
pytest                              # coverage gate fails the run below 100%
ruff check .                        # lint
ruff format --check .               # formatting — a separate gate from lint
python .github/check_doc_links.py   # relative links and heading anchors
```

**There is no CI.** Nothing runs these for you after you push, and no check
will appear on a pull request to tell you or a reviewer that something broke.
`pre-commit install` runs all four on every commit, which is the only
automation there is — install it, and do not `--no-verify` past it.

The project supports Python 3.9 and up. Without a build matrix, syntax newer
than 3.9 will not be caught before it reaches somebody's long-lived server;
`requires-python` in `pyproject.toml` is the floor to write against.

## Architecture in brief

`ldap_time_machine.py` → `ltm.pipeline.run()`. One run: acquire lock → fetch
LDIF via `ldapsearch` → parse → diff against yesterday → suppress flaps →
write snapshot and changes → render HTML → send. `--dry-run` does all of it
against a throwaway copy of the database and sends no mail.

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
ltm/cli.py            Argument parsing and entry point
ltm/_version.py       The version, single-sourced
```

## Hard rules

These are merge blockers, enforced by a human reviewer reading the diff —
there is no CI to catch any of them first. That makes the checklist at the
end of this file the whole gate, so run the commands and report what they
actually printed.

1. **Never hardcode a directory attribute name outside a flavor preset.** The
   code works in terms of *roles* (`username`, `display_name`, `country`).
   `ltm/config.py` maps each role to whatever the configured directory calls
   it, and `ltm/records.py` reads through that map. To add an attribute: add
   the role to `ATTRIBUTE_ROLES`, map it in **every** entry of `FLAVORS`, then
   read it with `records.value(record, "your_role")`. A literal
   `sAMAccountName` in a module silently breaks every OpenLDAP deployment
   while looking perfectly fine on Active Directory.

2. **Coverage stays at 100%, line and branch.** New code arrives with its
   tests. This is not a vanity number: the tool runs unattended, and the paths
   that matter most — a truncated fetch, a failed send, a malformed record —
   are exactly the ones nobody exercises by hand.

3. **Never loosen a gate to make a change pass.** Do not lower or remove
   `fail_under`, add entries to ruff's `ignore` list, add `# noqa` or
   `pragma: no cover`, narrow `testpaths`, or delete an assertion to get a
   green run. Weakening the gate is itself a rule violation, and a larger one
   than the failure it hides. If a branch is genuinely unreachable, delete the
   branch rather than exempting it.

4. **Run `ruff format --check .` as well as `ruff check .`.** Formatting is a
   separate gate; lint passing tells you nothing about it.

5. **Guardrails fail safe.** An empty or suspiciously small fetch aborts
   before writing. Never weaken a guardrail to make a scenario pass — a day of
   *wrong* history is worse than a missing day, because every later run treats
   it as the baseline.

6. **Everything from the directory is escaped** before it reaches HTML.

7. **No new heuristic without a config knob.** `leaver_confirm_days`,
   `flap_lookback_days`, `bulk_rename_min`, `min_ldap_result_ratio` and
   `unknown_ratio_limit` exist because directories lie temporarily. Their
   defaults suit a large, messy enterprise directory; every one can be
   disabled. Do not treat any of them as a fixed assumption.

8. **Nothing identifying belongs in the tree** — no real employer, person,
   hostname or internal detail, in code, fixtures, docs or commit messages.
   `tests/test_edge_cases.py` enforces this. Extend it when new material
   appears; never weaken it.

9. **The version lives only in `ltm/_version.py`.** `pyproject.toml` reads it
   dynamically. A test fails if a second copy is reintroduced.

## Invariants worth understanding before changing anything

- **Single-value attributes are "last wins."** Paged results can repeat a DN
  across a page boundary; accumulating those into lists merges two people into
  one record that still looks plausible.
- **Flap suppression excludes the current day.** Otherwise a re-run suppresses
  the changes it just wrote.
- **Leavers need continuous absence.** One missing day is a directory glitch,
  not a resignation.
- **Report sections fail independently.** A broken section blanks itself and
  logs; it does not abort the run.

## Task map

Read the referenced document before changing anything in that area. These
documents are the standard — prefer them over your own intuition about how a
Python project is usually laid out.

| Doing this | Read first |
| --- | --- |
| Adding or remapping a directory attribute | [docs/configuration.md#attribute-roles](docs/configuration.md#attribute-roles) |
| Adding a directory flavor (AD, OpenLDAP, …) | [docs/configuration.md#attribute-roles](docs/configuration.md#attribute-roles) |
| Adding or tuning a noise control | [docs/configuration.md#tuning-for-your-directory](docs/configuration.md#tuning-for-your-directory) |
| Changing which people are reported on | [docs/configuration.md#narrowing-the-population](docs/configuration.md#narrowing-the-population) |
| Changing what the report shows | [docs/configuration.md#report-presentation](docs/configuration.md#report-presentation) |
| Changing orchestration or run order | [docs/architecture.md#a-run-end-to-end](docs/architecture.md#a-run-end-to-end) |
| Touching SQL or the schema | [docs/architecture.md#database](docs/architecture.md#database) |
| Working inside a specific module | [docs/architecture.md#modules](docs/architecture.md#modules) |
| Questioning why something works as it does | [docs/architecture.md#design-decisions-worth-knowing](docs/architecture.md#design-decisions-worth-knowing) |
| Anything performance-related | [docs/architecture.md#performance](docs/architecture.md#performance) |
| Scheduling, monitoring, backups | [docs/operations.md](docs/operations.md) |
| Diagnosing a bad run or a wrong report | [docs/operations.md#troubleshooting](docs/operations.md#troubleshooting) |
| Writing or fixing tests | [CONTRIBUTING.md#tests](CONTRIBUTING.md#tests) and `tests/conftest.py` |
| Cutting a release | [CONTRIBUTING.md#releasing](CONTRIBUTING.md#releasing) |
| Changing a standalone tool | [tools/README.md](tools/README.md) |

## Tests

Prefer extending the sample history in `tests/conftest.py` over writing fresh
fixtures. It already contains a joiner, confirmed leavers, a one-day absence
that must not count as a departure, a person with a gap in their history, a
homonym pair, and a bulk rename — the edge cases are the point.

The suite avoids mocks where a real thing is cheap: real subprocesses against
a fake `ldapsearch` on `PATH`, a real SQLite database, real report rendering.
Only SMTP is stubbed.

## Before opening a PR

- [ ] `pytest` passes and coverage is still 100%
- [ ] `ruff check .` is clean
- [ ] `ruff format --check .` is clean
- [ ] `python .github/check_doc_links.py` is clean
- [ ] No gate was loosened to get there (rule 3)
- [ ] No directory attribute name outside a flavor preset (rule 1)
- [ ] New behaviour that could be unwanted has a config knob (rule 7)
- [ ] Nothing identifying added to code, fixtures, docs or commit messages
- [ ] Docs updated if behaviour or configuration changed
- [ ] `CHANGELOG.md` has an entry under `[Unreleased]`

This list is the same one in
[the pull request template](.github/PULL_REQUEST_TEMPLATE.md). If you are an
agent, verify each box against real command output rather than asserting it —
with no CI, an unchecked claim here is the last thing between a mistake and
`main`.

## Generated at runtime (not in git)

`ldap_time_machine.db`, `ldap_time_machine.log`, `ldap_time_machine.lock`,
`trend_graph.png`, `email_archive/`, `config.yml`, `credentials.yml`
