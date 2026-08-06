## What this changes

<!-- What it does and why. If it fixes an issue, link it. -->

## How you verified it

<!-- Paste the relevant command output, or describe the dry run. For anything
that changes the report, the archived HTML from email_archive/ is the fastest
evidence — with anything sensitive removed. -->

## Self-review

**There is no CI on this repository** — no check will appear below to catch
anything you miss. This checklist is the gate. Run the commands and tick from
the real output, not from memory:

```bash
pytest                              # coverage must still be 100%
ruff check .
ruff format --check .
python .github/check_doc_links.py
```

- [ ] `pytest` passes and coverage is still 100%
- [ ] `ruff check .` is clean
- [ ] `ruff format --check .` is clean
- [ ] `python .github/check_doc_links.py` is clean
- [ ] No gate was loosened to get there (no lowered `fail_under`, new ruff
      `ignore`, `# noqa`, `pragma: no cover`, or narrowed `testpaths`)
- [ ] No directory attribute name outside a flavor preset
- [ ] New behaviour that could be unwanted has a config knob
- [ ] Nothing identifying added to code, fixtures, docs or commit messages
- [ ] Docs updated if behaviour or configuration changed
- [ ] `CHANGELOG.md` has an entry under `[Unreleased]`

## If an AI agent wrote part of this

That is welcome, and held to exactly the same standards as anything else.
Point it at [AGENTS.md](../AGENTS.md) before it starts, and confirm the boxes
above against real command output — an agent asserting a suite passes is not
the same as the suite passing.
