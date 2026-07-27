# Configuration

Two files, both kept out of the repository:

| File | Contains | Default location |
|---|---|---|
| `config.yml` | Directory settings, report options, tuning | Repo root, or `~/.config/ldap-time-machine/config.yml`, or `$LTM_CONFIG` |
| `credentials.yml` | Bind password, SMTP settings | `~/.config/ldap-time-machine/credentials.yml`, mode 0600 |

Settings layer, later winning over earlier: built-in defaults → the directory
flavor preset → your `config.yml` → `LTM_*` environment variables.

`config.example.yml` documents every option inline; this page covers the
decisions behind them. Every credential can also be supplied as an
`LTM_`-prefixed variable (`LTM_LDAP_PASSWORD`, `LTM_SMTP_PASSWORD`), which is
how to inject secrets from a secret manager without writing them to disk.

---

## Attribute roles

The code never hardcodes an attribute name. It works in terms of **roles** —
`username`, `display_name`, `country` — and a flavor preset maps each role to
whatever your directory actually calls it:

| Role | `active_directory` | `openldap` | `generic` |
|---|---|---|---|
| `username` | `sAMAccountName` | `uid` | `uid` |
| `display_name` | `displayName` | `displayName` | — |
| `common_name` | `cn` | `cn` | `cn` |
| `email` | `mail` | `mail` | `mail` |
| `job_title` | `title` | `title` | `title` |
| `department` | `department` | `ou` | `ou` |
| `division` | `division` | `o` | `o` |
| `business_category` | `businessCategory` | `businessCategory` | `businessCategory` |
| `manager` | `manager` | `manager` | `manager` |
| `country` | `co` | `c` | `c` |
| `city` | `l` | `l` | `l` |
| `office` | `physicalDeliveryOfficeName` | `physicalDeliveryOfficeName` | `physicalDeliveryOfficeName` |
| `groups` | `memberOf` | `memberOf` | — |
| `created` | `whenCreated` | `createTimestamp` | — |
| `start_date` | — | — | — |

Pick the closest flavor, then override only what differs:

```yaml
ldap:
  flavor: active_directory
  attributes:
    start_date: employeeStartDate   # a schema extension, if you have one
    division: null                  # this directory has no division concept
```

Setting a role to `null` unmaps it. An unmapped role is not fetched, and every
report section that depends on it is skipped rather than rendering empty
columns. `start_date` is unmapped everywhere by default because no standard
schema has an HR start date — map it if yours does, and tenure will use
whichever of it and `created` is earlier.

`openldap` assumes the `memberof` overlay is loaded. Unmap `groups` if it is
not.

---

## Narrowing the population

`ldap.filter` is the most consequential setting in the file — it decides who
counts as a person.

Leave it unset to use the flavor default. The Active Directory default already
excludes computer accounts and disabled users:

```
(&(objectClass=user)(objectClass=person)
  (!(objectClass=computer))
  (!(userAccountControl:1.2.840.113556.1.4.803:=2)))
```

A common addition is a data-quality gate requiring some attribute to be set,
which drops service accounts and half-provisioned records:

```yaml
ldap:
  filter: "(&(objectClass=user)(objectClass=person)(!(objectClass=computer))(!(userAccountControl:1.2.840.113556.1.4.803:=2))(co=*))"
```

Changing this filter changes who is in the population, so the next run reports
everyone newly excluded as a leaver and everyone newly included as a joiner.
That is correct behaviour, but expect one noisy report after any filter change,
and consider whether the result-count guardrail will trip first.

---

## Report presentation

```yaml
report:
  org_name: "Acme"            # "Acme Seniority"; blank gives "Longest Tenured"
  member_label: "Employees"   # "Active Employees", "Top 50 Employees by ..."
  subject_prefix: "Directory Report"
  project_url: "https://..."  # a footer link; give readers somewhere to report
                              # a number that looks wrong
  highlights_enabled: false   # the four-tile strip; needs ~a month of history
  tracked_roles:              # which changes get their own section
    - job_title
    - department
    - manager
    - country                 # country and city render as one
    - city                    # combined "Location Changes" section
  top_n:
    countries: 25             # 0 hides a table entirely
    departments: 50
    seniority: 100
  max_table_rows: 500         # cap on any single people-table; 0 = unlimited
```

`max_table_rows` bounds the joiner, leaver, and change tables. It exists for
the first run against a large directory, where every person is technically a
joiner — uncapped, that is an email too large for most relays to accept.
Homonyms sort to the top, so the cap never hides the disambiguated rows.

Each breakdown table carries 30-day, 180-day and 1-year deltas — see the
[example in the README](../README.md#the-daily-email).

A delta is a comparison of two snapshots: today's count against the count on
the nearest run at or before 30, 180, or 365 days ago. It is not a sum of
changes made during the window — so a department created yesterday shows its
whole headcount as growth in every column, because every baseline predates it.

A delta reads `N/A` when the database does not reach back that far, and a dash
when the value genuinely has not moved. Neither is shown as `0`, which would be
indistinguishable from the other two.

---

## Tuning for your directory

Directories lie temporarily. A sync blips and a thousand people vanish for an
hour. An attribute oscillates between two values for a week. A reorganisation
renames a department and buries the three individual moves that mattered.

Several settings exist purely to absorb that. Their defaults assume a large,
busy, imperfect enterprise directory:

| Setting | Default | What it absorbs | Off |
|---|---|---|---|
| `windows.leaver_confirm_days` | 7 | A sync outage reporting everyone as resigned | `1` |
| `windows.flap_lookback_days` | 14 | A → B → A attribute oscillation | `0` |
| `windows.bulk_rename_min` | 3 | A reorg burying individual moves | `0` |
| `guardrails.min_ldap_result_ratio` | 0.90 | A truncated fetch being stored as fact | `0` |
| `guardrails.unknown_ratio_limit` | 0.90 | A newly-populated attribute reading as growth | `1.0` |

On a small or well-curated directory these are too conservative and will delay
or hide real changes. Start permissive, watch a week of reports, and tighten
only what turns out to be noisy.

**None of them change what is stored — only what is reported.** You can retune
at any time without losing history: a change these settings kept out of a
report is still in the `changes` table, queryable directly — and
`ldap-time-machine --replay YYYY-MM-DD` re-renders any past day's report
under the current settings.

### What each one actually does

**`leaver_confirm_days`** — someone must be absent from *every* run in the
window before they are called a leaver. At 7, a departure is reported a week
late but a directory outage never reports the whole company as gone. At 1,
departures are same-day and every blip is a false alarm.

**`flap_lookback_days`** — a change is suppressed if the incoming value matches
either side of any change already recorded for that person and attribute inside
the window. Today's own changes are excluded, so re-running never suppresses
what it just wrote.

**`bulk_rename_min`** — people sharing one old → new department pair collapse
into a single row listing up to `bulk_rename_preview_limit` names and "… and N
more". Individual moves below the threshold are still listed in full.

**`min_ldap_result_ratio`** — if today's count falls below this fraction of the
previous snapshot, the run aborts *before writing anything*. A truncated fetch
stored as fact becomes the baseline every later run trusts, and tomorrow's
report announces thousands of departures.

**`unknown_ratio_limit`** — an attribute's historical counts are hidden when
more than this fraction were unknown on that date. Otherwise the day you start
populating an attribute appears in every trend column as explosive growth.
