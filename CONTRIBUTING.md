# Contributing

Thanks for taking an interest.

## Getting set up

```bash
git clone https://github.com/jbrooks84/ldap_time_machine.git
cd ldap_time_machine
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pre-commit install
```

## Before opening a pull request

```bash
pytest                    # the full suite; coverage must stay at 100%
ruff check .
ruff format .
```

`pre-commit install` runs all of that automatically on commit, and CI runs it
again across Python 3.9 through 3.13.

## What the project cares about

**Nothing directory-specific in the code.** Attribute names live in the
configuration, never in a module. If you need a new one, add a *role* in
`ltm/config.py`, map it in each flavor preset, and read it through
`ltm/records.py`. A literal `sAMAccountName` anywhere outside a preset or a
test fixture is a bug — it silently breaks every non-AD deployment.

**Coverage stays at 100%.** Not as a vanity number: this runs unattended
against real directories, and the paths that matter most — a truncated fetch,
a failed send, a malformed record — are exactly the ones nobody exercises by
hand. If a branch is genuinely unreachable, delete it rather than exempting it.

**Guardrails fail safe.** When something is uncertain, the tool should decline
to write rather than write something wrong. A missed day of history is
recoverable; a day of *wrong* history becomes the baseline every later run
trusts.

**Comments explain why.** The code says what it does. Comments are for the
reasoning that is not visible from the code — the bug a check prevents, the
reason a default is what it is, the failure mode being defended against.

## Tests

The suite avoids mocks where a real thing is cheap. It runs actual subprocesses
against a fake `ldapsearch` on `PATH`, a real SQLite database, and real report
rendering. Only SMTP is stubbed.

`tests/conftest.py` builds a 40-day sample directory history containing a
joiner, two confirmed leavers, a one-day absence that must *not* count as a
departure, someone with a gap in their history, a pair of people sharing a
display name, and a bulk department rename. Prefer extending that fixture over
writing three hand-made rows — the edge cases are the point.

## Releasing

The version is declared once, in `ltm/_version.py`. `pyproject.toml` reads it
dynamically, so bumping a release means editing that one line — there is no
second place to keep in sync, and a test fails if one is reintroduced.

1. Bump `__version__` in `ltm/_version.py`.
2. Move the `[Unreleased]` entries in `CHANGELOG.md` under the new version.
3. Commit, tag `vX.Y.Z`, push the tag.
4. `gh release create vX.Y.Z --notes-from-tag` (attach `dist/*` if publishing
   artifacts).

## Reporting a bug

Please include the directory type and version, the relevant `config.yml`
section (**without** credentials), and the log lines around the failure.

If it concerns a report being wrong rather than a crash, the archived HTML from
`email_archive/` is usually the fastest way to see what happened — again, with
anything sensitive removed.

## Security

Please do not open a public issue for a security problem. Contact the
maintainer directly.
