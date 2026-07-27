"""HTML report generation and delivery.

This module owns everything between "the pipeline has its data" and "the mail
is on its way":

  - the trend graph PNG (``generate_trend_graph``),
  - the aggregate count data behind the breakdown tables,
  - the HTML document itself (``build_report_html`` and the section helpers),
  - archiving the rendered HTML to disk whether or not mail is sent, which is
    what makes ``--dry-run`` useful,
  - SMTP delivery (``send_email_report``).

Nothing here hardcodes a directory attribute name.  Sections are built from
*roles* — ``job_title``, ``department``, ``country`` — and a section whose
role the configured directory does not map simply does not render.  That is
what lets the same report work against Active Directory and OpenLDAP.

Section renderers follow one pattern: they take processed data, return an
HTML string or ``''`` so callers can join them without guarding, and escape
every value that came from the directory.  Two builders are the exception —
``build_aggregate_sections`` and ``build_highlights_strip`` take the open
connection, because their sections are derived from historical baselines
rather than data the pipeline already holds.

Failure isolation is deliberate and layered.  A broken tile blanks one tile, a
broken section blanks one section, and a failed SMTP send still leaves a valid
archived report on disk.  A daily report that is 90% right beats no report.
"""

import collections
import html
import logging
import os
import smtplib
from datetime import datetime, timedelta
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from . import records
from .config import (
    ARCHIVE_KEEP,
    ARCHIVE_KEEP_DRY_RUN,
    ATTR,
    BULK_RENAME_MIN,
    BULK_RENAME_PREVIEW_LIMIT,
    EMAIL_ARCHIVE_DIR,
    HIGHLIGHTS_COUNTRY_MIN_BASELINE,
    HIGHLIGHTS_ENABLED,
    HIGHLIGHTS_LOOKBACK_DAYS,
    HIGHLIGHTS_OFFICE_MIN_BASELINE,
    MAX_TABLE_ROWS,
    MEMBER_LABEL,
    ORG_NAME,
    PROJECT_URL,
    SMTP_TIMEOUT_SECONDS,
    SUBJECT_PREFIX,
    TOP_N,
    TRACKED_ROLES,
    TREND_GRAPH_FILE,
    VERSION,
)
from .dates import parse_join_date, tenure_str
from .db import counts_from_records, get_attribute_counts, get_run_date_counts
from .highlights import (
    biggest_country_mover,
    biggest_office_mover,
    headcount_deltas,
    longest_tenured_leaver,
)
from .labels import window_label, window_label_short
from .ldif_parser import smart_clean_val
from .secrets import get_smtp_settings

# Matplotlib must run headless — there is no display on a scheduled host.
plt.switch_backend("Agg")

# Human-readable names for roles, used in table headings and column headers.
ROLE_LABELS = {
    "username": "Username",
    "display_name": "Name",
    "common_name": "Name",
    "email": "Email",
    "job_title": "Title",
    "department": "Department",
    "division": "Division",
    "business_category": "Business Category",
    "manager": "Manager",
    "country": "Country",
    "city": "City",
    "office": "Office",
    "groups": "Groups",
    "created": "Created",
    "start_date": "Start Date",
}

ROLE_EMOJI = {
    "job_title": "👔",
    "department": "📊",
    "division": "🏢",
    "business_category": "🧭",
    "manager": "👥",
    "country": "🌎",
    "city": "🌎",
    "office": "📍",
    "email": "✉️",
    "groups": "🔑",
}

# Aggregate breakdown tables: (role, top_n config key, emoji, plural label).
# A table is skipped when its role is unmapped or its row cap is 0.
AGGREGATE_TABLES = (
    ("country", "countries", "🌎", "Countries"),
    ("office", "offices", "📍", "Work Locations"),
    ("department", "departments", "📊", "Departments"),
    ("job_title", "job_titles", "👔", "Titles"),
    ("division", "divisions", "🏢", "Divisions"),
    ("business_category", "business_categories", "🧭", "Business Categories"),
)


def role_label(role):
    """Return a human-readable label for a role."""
    return ROLE_LABELS.get(role, role.replace("_", " ").title())


def role_emoji(role):
    """Return a leading emoji for a role's section heading."""
    return ROLE_EMOJI.get(role, "🔁")


def _attr_code(role):
    """Return the directory attribute name as an inline <code> snippet, or ''.

    Reports name the attribute they are derived from, so a reader who
    disagrees with a number knows exactly what to query.
    """
    attribute = ATTR.get(role)
    return f"<code>{html.escape(attribute)}</code>" if attribute else ""


# ──────────────────────────────────────────────
# Homonyms
# ──────────────────────────────────────────────


def find_homonym_names(new_users, removed_users, modifications):
    """Return display names appearing 2+ times across the report's sections.

    Two people with the same display name in one report is not a bug and not
    rare at any scale — the most common shape is one leaving as a namesake
    joins.  Rows for these names get highlighted and badged so a reader is
    never silently looking at the wrong person.
    """
    names = []
    for record in (new_users or []) + (removed_users or []):
        name = records.display_name(record)
        if name:
            names.append(name)
    for mod in modifications or []:
        name = (mod.get("name") or "").strip()
        if name:
            names.append(name)
    counts = collections.Counter(names)
    return {name for name, count in counts.items() if count >= 2}


def _row_style(is_homonym):
    """Return the inline style for a homonym-flagged row, or ''.

    Inline rather than a CSS class because several mail clients discard
    <style> blocks entirely.
    """
    return ' style="background-color:#fff8e1;"' if is_homonym else ""


def _cap_rows(rows):
    """Trim a table's rows to the configured maximum.

    Returns (rows_to_render, hidden_count). MAX_TABLE_ROWS <= 0 disables the
    cap. Callers sort before capping, so what survives is deterministic — and
    because homonyms sort to the top, the disambiguated rows are never the
    ones dropped.
    """
    if MAX_TABLE_ROWS <= 0 or len(rows) <= MAX_TABLE_ROWS:
        return rows, 0
    return rows[:MAX_TABLE_ROWS], len(rows) - MAX_TABLE_ROWS


def _truncation_note(hidden, total):
    """Explain a capped table, or '' when nothing was hidden.

    This exists for the first run against a large directory, where every
    person is technically a joiner: without a cap that is a report measured
    at 9 MB of HTML for 50,000 people — past the size most mail relays
    accept, so the first report would simply never arrive. The data is all
    stored regardless; only the rendering is capped.
    """
    if not hidden:
        return ""
    return (
        "<p style='color:#888; font-size:11px; margin-top:5px;'>"
        f"Showing {total - hidden:,} of {total:,} rows. The rest are recorded "
        "in the database; raise <code>report.max_table_rows</code> to widen "
        "this table.</p>"
    )


def _name_with_badge(display_name, username, is_homonym):
    """Return a name cell, appending a username badge when the name is ambiguous.

    The badge only appears for homonyms, so the common case stays uncluttered.
    """
    safe_name = html.escape(display_name)
    if is_homonym and username:
        return f"{safe_name} <span class='badge'>{html.escape(username)}</span>"
    return safe_name


def _homonym_footnote(names_in_table, homonym_names):
    """Return the explanatory footnote if this table contains a homonym, else ''."""
    if not homonym_names:
        return ""
    if not any(name in homonym_names for name in names_in_table):
        return ""
    return (
        "<p style='color:#888; font-size:11px; margin-top:5px;'>"
        "Rows highlighted in amber share a display name in this report — "
        "see the username to tell them apart.</p>"
    )


# ──────────────────────────────────────────────
# Highlights strip
# ──────────────────────────────────────────────


def _safe_call(fn, *args, **kwargs):
    """Call a tile function, returning None if it raises.

    One tile failing must not blank the other three.  The function name is
    logged so the failure can be investigated without reproducing the run.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logging.error(
            "Highlight tile %s failed: %s", getattr(fn, "__name__", str(fn)), e
        )
        return None


def build_highlights_strip(conn, today_str, leavers):
    """Return the rendered highlights strip, or '' on failure.

    Each tile is computed independently, so a missing baseline — a database
    too young for a 30-day comparison — blanks only the affected tile.
    """
    try:
        today_dt = datetime.strptime(today_str, "%Y-%m-%d")
        tile_a = _safe_call(longest_tenured_leaver, leavers, today_dt)
        tile_b = _safe_call(
            biggest_country_mover,
            conn,
            today_str,
            HIGHLIGHTS_LOOKBACK_DAYS,
            HIGHLIGHTS_COUNTRY_MIN_BASELINE,
        )
        country_tuple = (tile_b["past"], tile_b["now"]) if tile_b else None
        tile_c = _safe_call(
            biggest_office_mover,
            conn,
            today_str,
            HIGHLIGHTS_LOOKBACK_DAYS,
            HIGHLIGHTS_OFFICE_MIN_BASELINE,
            exclude_country_tuple=country_tuple,
        )
        tile_d = _safe_call(headcount_deltas, conn, today_str)
        return render_highlights_html(tile_a, tile_b, tile_c, tile_d)
    except Exception as e:
        logging.error("build_highlights_strip failed: %s", e)
        return ""


def _signed(value):
    """Format a number with an explicit leading sign."""
    return f"+{value}" if value >= 0 else str(value)


def render_highlights_html(tile_a, tile_b, tile_c, tile_d):
    """Render the four-column highlights table from pre-computed tile data.

    A tile that is None renders as an em dash so the layout survives partial
    data.
    """

    def cell(label, value_html):
        return (
            "<td style='vertical-align:top; padding:8px 14px; width:25%;'>"
            "<div style='font-size:11px; color:#888; text-transform:uppercase; "
            f"letter-spacing:0.5px; margin-bottom:4px;'>{html.escape(label)}</div>"
            f"<div style='font-size:14px; line-height:1.4; color:#333;'>"
            f"{value_html}</div></td>"
        )

    if tile_a:
        a_value = (
            f"{html.escape(tile_a['name'])} · <b>{html.escape(tile_a['tenure'])}</b>"
        )
        if tile_a["country"]:
            a_value += f" · {html.escape(str(tile_a['country']))}"
    else:
        a_value = "<span style='color:#2e7d32;'>No confirmed leavers ✓</span>"

    def mover_value(tile, key):
        if not tile:
            return "—"
        return (
            f"{html.escape(str(tile[key]))}"
            f" · {tile['past']} → <b>{tile['now']}</b>"
            f" ({_signed(round(tile['pct']))}%)"
            f" <span style='color:#888;'>· {html.escape(tile['days_label'])}</span>"
        )

    if tile_d:
        parts = [f"<b>{tile_d['now']:,}</b> active"]
        if tile_d["delta_30"] is not None:
            parts.append(f"{_signed(tile_d['delta_30'])} (30d)")
        if tile_d["delta_90"] is not None:
            parts.append(f"{_signed(tile_d['delta_90'])} (90d)")
        d_value = " · ".join(parts)
    else:
        d_value = "—"

    return (
        "<div style='margin-bottom:25px; border-bottom:1px solid #eee; "
        "padding-bottom:20px;'>"
        "<div style='font-size:11px; color:#888; text-transform:uppercase; "
        "letter-spacing:0.5px; margin-bottom:8px;'>Highlights</div>"
        "<table style='width:100%; border-collapse:collapse;'>"
        f"<tr>{cell('Longest-tenured leaver', a_value)}"
        f"{cell('Country mover', mover_value(tile_b, 'country'))}"
        f"{cell('Office mover', mover_value(tile_c, 'office'))}"
        f"{cell('Net headcount', d_value)}</tr>"
        "</table></div>"
    )


# ──────────────────────────────────────────────
# Attribute change sections
# ──────────────────────────────────────────────


def split_changes_by_role(modifications):
    """Reshape per-person modifications into per-role lists.

    The pipeline produces changes grouped by person; the report renders them
    grouped by what changed.  This is that transposition.

    Changes whose attribute maps to no configured role are dropped — that only
    happens if the attribute map changed after the rows were written, and a
    section header of the raw attribute name would confuse more than it helps.

    Args:
        modifications: [{name, username, changes: [{attr, old, new}, ...]}]

    Returns:
        {role: [{name, username, old, new}, ...]} for tracked roles only.
    """
    out = {role: [] for role in TRACKED_ROLES}
    for mod in modifications or []:
        for change in mod.get("changes", []):
            role = ATTR.role_of(change.get("attr"))
            if role not in out:
                continue
            out[role].append(
                {
                    "name": mod.get("name", ""),
                    "username": mod.get("username", ""),
                    "old": change.get("old", ""),
                    "new": change.get("new", ""),
                }
            )
    return out


def group_dept_changes(dept_changes, threshold):
    """Split department changes into bulk renames and individual moves.

    A reorganisation renames whole departments at once, and listing every
    affected person turns the report into a wall of names that hides the few
    individual moves that a reader actually needs to see.  Groups of
    ``threshold`` or more people sharing one (old, new) pair collapse into a
    single row.

    Args:
        dept_changes: [{name, username, old, new}]
        threshold: Minimum group size to qualify as a bulk rename.

    A threshold of 0 or less disables roll-up entirely — every move is listed
    individually, which is what a directory small enough to read in full wants.

    Returns:
        (bulk_renames, individual_moves).  bulk_renames is a list of
        {old, new, count, members} sorted by count descending, with members
        retained so the row can still name who moved.
    """
    if threshold <= 0:
        return [], list(dept_changes or [])

    groups = collections.defaultdict(list)
    for change in dept_changes or []:
        groups[(change.get("old", ""), change.get("new", ""))].append(change)

    bulk = []
    individual = []
    for (old, new), members in groups.items():
        if len(members) >= threshold:
            bulk.append(
                {
                    "old": old,
                    "new": new,
                    "count": len(members),
                    "members": sorted(members, key=lambda m: m.get("name", "").lower()),
                }
            )
        else:
            individual.extend(members)
    bulk.sort(key=lambda b: -b["count"])
    return bulk, individual


def format_bulk_members(members, homonym_names, preview_limit):
    """Render the inline name list for a bulk-rename row.

    A single reorganisation pair can cover hundreds of people.  Show up to
    ``preview_limit`` names, then summarise the remainder.
    """
    if not members:
        return ""
    rendered = [
        _name_with_badge(
            m.get("name", "").strip(),
            m.get("username", ""),
            m.get("name", "").strip() in homonym_names,
        )
        for m in members
    ]
    if len(rendered) <= preview_limit:
        return ", ".join(rendered)
    head = ", ".join(rendered[:preview_limit])
    return (
        f"{head} <i style='color:#888;'>… and {len(rendered) - preview_limit} more</i>"
    )


def _change_rows(items, role, homonym_names):
    """Render the <tr> rows shared by every simple change table."""
    attribute = ATTR.get(role, role)
    rows = ""
    for item in items:
        name = item["name"].strip()
        is_homonym = name in homonym_names
        username = item.get("username", "")
        rows += (
            f"<tr{_row_style(is_homonym)}>"
            f"<td><b>{_name_with_badge(name, username, is_homonym)}</b></td>"
            f"<td>{html.escape(username)}</td>"
            f"<td>{html.escape(smart_clean_val(attribute, item['old']))} &rarr; "
            f"<b>{html.escape(smart_clean_val(attribute, item['new']))}</b></td>"
            "</tr>"
        )
    return rows


def render_role_changes(role, items, window_long, homonym_names=None):
    """Render a generic "<Role> Changes" table, or '' when there is nothing.

    Used for every tracked role that does not have a purpose-built renderer.
    """
    if not items:
        return ""
    homonym_names = homonym_names or set()
    label = role_label(role)
    code = _attr_code(role)
    suffix = f" {code} attribute." if code else ""
    shown, hidden = _cap_rows(items)
    return (
        f"<h3>{role_emoji(role)} {label} Changes</h3>"
        "<p style='color:#666; font-size:12px; margin:-10px 0 15px 0;'>"
        f"{label} changes in the {window_long}.{suffix}</p>"
        "<table><tr><th style='width:30%'>Name</th>"
        "<th style='width:20%'>Username</th><th>From → To</th></tr>"
        f"{_change_rows(shown, role, homonym_names)}</table>"
        f"{_truncation_note(hidden, len(items))}"
        f"{_homonym_footnote([i['name'].strip() for i in shown], homonym_names)}"
    )


def render_dept_changes(items, threshold, window_long, homonym_names=None):
    """Render the department section, with bulk renames rolled up."""
    if not items:
        return ""
    homonym_names = homonym_names or set()
    bulk, individual = group_dept_changes(items, threshold)
    label = role_label("department")
    code = _attr_code("department")

    rollup_note = (
        f" Mass renames (≥{threshold} people sharing the same old → new pair) "
        "are rolled up."
        if threshold > 0
        else ""
    )
    out = [
        f"<h3>{role_emoji('department')} {label} Changes</h3>",
        "<p style='color:#666; font-size:12px; margin:-10px 0 15px 0;'>"
        f"{label} changes in the {window_long}.{rollup_note}"
        f"{' ' + code + ' attribute.' if code else ''}</p>",
    ]

    if bulk:
        out.append("<h4 style='margin-top:18px;'>Bulk renames</h4>")
        out.append(
            "<table><tr><th style='width:8%'>People</th>"
            "<th style='width:42%'>From → To</th><th>Who</th></tr>"
        )
        for group in bulk:
            who = format_bulk_members(
                group.get("members", []), homonym_names, BULK_RENAME_PREVIEW_LIMIT
            )
            out.append(
                f"<tr><td><b>{group['count']}</b></td>"
                f"<td>{html.escape(group['old'])} &rarr; "
                f"<b>{html.escape(group['new'])}</b></td>"
                f"<td>{who}</td></tr>"
            )
        out.append("</table>")

    if individual:
        out.append("<h4 style='margin-top:18px;'>Individual moves</h4>")
        out.append(
            "<table><tr><th style='width:30%'>Name</th>"
            "<th style='width:20%'>Username</th><th>From → To</th></tr>"
        )
        shown, hidden = _cap_rows(individual)
        out.append(_change_rows(shown, "department", homonym_names))
        out.append("</table>")
        out.append(_truncation_note(hidden, len(individual)))
        out.append(
            _homonym_footnote([i["name"].strip() for i in individual], homonym_names)
        )
    return "".join(out)


def render_location_changes(country_items, city_items, window_long, homonym_names=None):
    """Render country and city changes as one section.

    Merged by person so someone who moved country and city on the same day
    appears once, not twice.
    """
    if not country_items and not city_items:
        return ""
    homonym_names = homonym_names or set()

    by_person = {}
    for role, items in (("country", country_items), ("city", city_items)):
        for item in items or []:
            key = (item["name"], item.get("username", ""))
            by_person.setdefault(key, {})[role] = (item["old"], item["new"])

    codes = " and ".join(c for c in (_attr_code("country"), _attr_code("city")) if c)
    out = [
        "<h3>🌎 Location Changes</h3>",
        "<p style='color:#666; font-size:12px; margin:-10px 0 15px 0;'>"
        f"Location changes in the {window_long}."
        f"{' ' + codes + ' attributes.' if codes else ''}</p>",
        "<table><tr><th style='width:30%'>Name</th>"
        "<th style='width:20%'>Username</th><th>Change</th></tr>",
    ]

    pairs, hidden = _cap_rows(list(by_person.items()))
    for (name, username), changes in pairs:
        clean_name = name.strip()
        is_homonym = clean_name in homonym_names
        parts = []
        for role in ("country", "city"):
            if role not in changes:
                continue
            attribute = ATTR.get(role, role)
            old_val, new_val = changes[role]
            parts.append(
                f"<code>{html.escape(attribute)}</code>: "
                f"{html.escape(smart_clean_val(attribute, old_val))} &rarr; "
                f"<b>{html.escape(smart_clean_val(attribute, new_val))}</b>"
            )
        out.append(
            f"<tr{_row_style(is_homonym)}>"
            f"<td><b>{_name_with_badge(clean_name, username, is_homonym)}</b></td>"
            f"<td>{html.escape(username)}</td>"
            f"<td>{' &nbsp; · &nbsp; '.join(parts)}</td></tr>"
        )
    out.append("</table>")
    out.append(_truncation_note(hidden, len(by_person)))
    out.append(_homonym_footnote([k[0].strip() for k in by_person], homonym_names))
    return "".join(out)


def render_attribute_changes(modifications, threshold, window_long, homonym_names=None):
    """Render every tracked-role change section, isolating per-section failures.

    Department and location get purpose-built renderers; every other tracked
    role gets the generic table.  Each renderer runs inside its own try/except
    so one raising cannot suppress the rest.
    """
    try:
        by_role = split_changes_by_role(modifications)
    except Exception as e:
        logging.error("Attribute split failed: %s", e)
        return ""

    jobs = []
    for role in TRACKED_ROLES:
        if role == "department":
            jobs.append(
                (
                    "department",
                    render_dept_changes,
                    (
                        by_role.get("department", []),
                        threshold,
                        window_long,
                        homonym_names,
                    ),
                )
            )
        elif role == "country":
            # City is folded into the country section; skip it on its own.
            jobs.append(
                (
                    "location",
                    render_location_changes,
                    (
                        by_role.get("country", []),
                        by_role.get("city", []),
                        window_long,
                        homonym_names,
                    ),
                )
            )
        elif role == "city" and "country" in TRACKED_ROLES:
            continue
        elif role == "city":
            jobs.append(
                (
                    "location",
                    render_location_changes,
                    ([], by_role.get("city", []), window_long, homonym_names),
                )
            )
        else:
            jobs.append(
                (
                    role,
                    render_role_changes,
                    (role, by_role.get(role, []), window_long, homonym_names),
                )
            )

    out = []
    for name, renderer, args in jobs:
        try:
            out.append(renderer(*args))
        except Exception as e:
            logging.error("Failed to render %s changes: %s", name, e)
    return "\n".join(section for section in out if section)


# ──────────────────────────────────────────────
# Trend graph
# ──────────────────────────────────────────────


def generate_trend_graph(conn, path=None, end_date_str=None):
    """Render a headcount-over-time PNG, returning True if one was written.

    ``AutoDateLocator`` with ``ConciseDateFormatter`` scales the x-axis from a
    few days to several years without any manual tick handling, so the graph
    stays readable as history accumulates.

    ``end_date_str`` truncates the series: a ``--replay`` of a past date must
    not draw history that had not happened yet.  For a normal nightly run the
    report date is the newest date on file, so the filter changes nothing.

    Returns False when there is no data yet — the caller then omits the image
    rather than embedding a broken one.  Never raises.
    """
    path = path or TREND_GRAPH_FILE
    data = get_run_date_counts(conn)
    if end_date_str:
        data = [row for row in data if row[0] <= end_date_str]
    if not data:
        return False

    dates = [datetime.strptime(row[0], "%Y-%m-%d") for row in data]
    counts = [row[1] for row in data]

    plt.figure(figsize=(12, 4))
    plt.plot(
        dates,
        counts,
        marker="o",
        linestyle="-",
        color="#0056b3",
        linewidth=2,
        markersize=5,
    )

    # Pad the y-axis so the line never sits flush against the frame. Scaled to
    # the data so a 50-person directory and a 50,000-person one both look right.
    padding = max(round((max(counts) - min(counts)) * 0.1), 1)
    y_min = min(counts) - padding
    plt.ylim(y_min, max(counts) + padding)
    plt.fill_between(dates, counts, y_min, color="#0056b3", alpha=0.1)

    plt.title(
        f"Total Active {MEMBER_LABEL} Over Time",
        fontsize=14,
        fontweight="bold",
        color="#333333",
    )
    plt.grid(True, linestyle="--", alpha=0.5)
    locator = mdates.AutoDateLocator(minticks=6, maxticks=12)
    plt.gca().xaxis.set_major_locator(locator)
    plt.gca().xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    plt.xticks(rotation=0, fontsize=10)
    plt.tight_layout()
    plt.savefig(path, dpi=100)
    plt.close()
    return True


# ──────────────────────────────────────────────
# Aggregate tables
# ──────────────────────────────────────────────


def format_delta(current, past):
    """Format a headcount delta as a coloured span.

    ``N/A`` when there is no baseline, a green ``+N`` for growth, a red ``-N``
    for decline, and a dash for no change — a literal ``0`` reads as missing
    data at a glance, which is exactly the wrong impression.
    """
    if past is None:
        return "<span style='color:#999'>N/A</span>"
    diff = current - past
    if diff > 0:
        return f"<span style='color:green; font-weight:bold;'>+{diff}</span>"
    if diff < 0:
        return f"<span style='color:red; font-weight:bold;'>{diff}</span>"
    return "<span style='color:#ccc'>-</span>"


def build_aggregate_table(
    title,
    emoji,
    description,
    items,
    total_count,
    counts_30,
    counts_180,
    counts_365,
    top_n=10,
    col_header=None,
):
    """Build a ranked breakdown table with 30d / 180d / 1yr trend columns.

    Args:
        title: Section title, without the emoji.
        emoji: Emoji prefix for the heading.
        description: Subtitle paragraph.
        items: {value: current_count}, already filtered.
        total_count: Denominator for the percentage column.
        counts_30/counts_180/counts_365: Historical {value: count}, or None
            when that baseline does not exist.
        top_n: Maximum rows.
        col_header: Override for the first column header.

    Returns:
        An HTML string, or '' when there is nothing to show.
    """
    if not items or top_n <= 0:
        return ""

    # Descending count, then name: the tiebreak makes row order fully
    # deterministic, so two reports over the same data always diff clean —
    # without it, equal counts landed in whatever order the counting source
    # happened to iterate.
    ranked = sorted(items.items(), key=lambda kv: (-kv[1], str(kv[0])))[:top_n]
    rows = ""
    for name, count in ranked:
        pct = (count / total_count) * 100 if total_count else 0
        rows += (
            f"<tr><td>{html.escape(str(name))}</td><td><b>{count}</b></td>"
            f"<td>{pct:.1f}%</td>"
            f"<td>{format_delta(count, counts_30.get(name, 0) if counts_30 else None)}</td>"
            f"<td>{format_delta(count, counts_180.get(name, 0) if counts_180 else None)}</td>"
            f"<td>{format_delta(count, counts_365.get(name, 0) if counts_365 else None)}</td>"
            "</tr>"
        )

    if col_header is None:
        col_header = _singularise(title.split()[-1])

    return f"""
            <div style="margin-top:40px;">
                <h3>{emoji} {html.escape(title)}</h3>
                <p style="color:#666; font-size:12px; margin:-10px 0 15px 0;">{description}</p>
                <table>
                    <tr><th style="width:40%">{html.escape(col_header)}</th><th>Count</th><th>%</th><th>30d</th><th>180d</th><th>1yr</th></tr>
                    {rows}
                </table>
            </div>"""


def _singularise(word):
    """Return a naive singular form, for deriving a column header from a title."""
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("s"):
        return word[:-1]
    return word


def build_aggregate_sections(conn, today_str, total_count, current_state):
    """Build every configured breakdown table.

    Today's counts come straight from ``current_state`` — the same records the
    pipeline just wrote — rather than re-reading the day's rows and
    json_extract-ing every blob back out of the database. The three
    historical baselines still come from the database, one query per date
    covering every attribute at once.

    Returns:
        The concatenated HTML for the aggregate tables.
    """
    active = [
        (role, key, emoji, label)
        for role, key, emoji, label in AGGREGATE_TABLES
        if role in ATTR and TOP_N.get(key, 0) > 0
    ]
    if not active:
        return ""

    attributes = ATTR.names([role for role, _, _, _ in active])
    today_dt = datetime.strptime(today_str, "%Y-%m-%d")

    def counts_at(days):
        target = (today_dt - timedelta(days=days)).strftime("%Y-%m-%d")
        return get_attribute_counts(conn, target, attributes)

    counts_now = counts_from_records(current_state.values(), attributes)
    counts_30 = counts_at(30)
    counts_180 = counts_at(180)
    counts_365 = counts_at(365)

    sections = []
    for role, key, emoji, label in active:
        attribute = ATTR.get(role)
        current = counts_now.get(attribute) or {}
        # "Unknown" is an absence of data, not a value worth ranking.
        current = {k: v for k, v in current.items() if k and k != "Unknown"}
        if not current:
            continue
        code = _attr_code(role)
        top_n = TOP_N[key]
        sections.append(
            build_aggregate_table(
                f"Top {top_n} {label}",
                emoji,
                f"{MEMBER_LABEL} by {role_label(role).lower()}."
                f"{' ' + code + ' attribute.' if code else ''}",
                current,
                total_count,
                counts_30.get(attribute),
                counts_180.get(attribute),
                counts_365.get(attribute),
                top_n,
                col_header=role_label(role),
            )
        )
    return "\n".join(sections)


# ──────────────────────────────────────────────
# People tables
# ──────────────────────────────────────────────


def _sort_key(record, homonym_names):
    """Sort homonyms to the top, then alphabetically.

    Ambiguous names are the rows most likely to be misread, so they go where
    they will be seen rather than buried mid-table.
    """
    name = records.display_name(record)
    return (0 if name in homonym_names else 1, name, records.username(record))


def build_joiner_table(title, users, roles, description="", homonym_names=None):
    """Render the joiners table, or '' when there are none."""
    if not users:
        return ""
    homonym_names = homonym_names or set()

    out = [f"<h3>{title}</h3>"]
    if description:
        out.append(
            "<p style='color:#666; font-size:12px; margin:-10px 0 15px 0;'>"
            f"{description}</p>"
        )
    out.append("<table><tr>")
    out.append("<th>Name</th><th>Username</th>")
    for role in roles:
        out.append(f"<th>{html.escape(role_label(role))}</th>")
    out.append("</tr>")

    ordered = sorted(users, key=lambda r: _sort_key(r, homonym_names))
    shown, hidden = _cap_rows(ordered)
    for record in shown:
        name = records.display_name(record)
        username = records.username(record)
        is_homonym = name in homonym_names
        out.append(f"<tr{_row_style(is_homonym)}>")
        out.append(f"<td>{_name_with_badge(name, username, is_homonym)}</td>")
        out.append(f"<td>{html.escape(username)}</td>")
        for role in roles:
            attribute = ATTR.get(role, role)
            value = smart_clean_val(attribute, records.value(record, role))
            out.append(f"<td>{html.escape(value)}</td>")
        out.append("</tr>")
    out.append("</table>")
    out.append(_truncation_note(hidden, len(users)))
    out.append(
        _homonym_footnote([records.display_name(u) for u in users], homonym_names)
    )
    return "".join(out)


def build_leaver_table(
    title, users, flapper_dns=None, confirm_days=None, homonym_names=None, as_of=None
):
    """Render the confirmed-leavers table with tenure and flapper marks.

    Leavers who have previously dropped out and returned are marked with an
    asterisk, because for those people "gone" has been wrong before.

    ``as_of`` anchors the tenure arithmetic; a replayed report must state the
    tenure someone had on the replayed date, not today.
    """
    if not users:
        return ""
    flapper_dns = flapper_dns or {}
    homonym_names = homonym_names or set()
    now = as_of or datetime.now()

    detail_roles = [r for r in ("job_title", "department", "country") if r in ATTR]

    confirm_text = (
        f" Confirmed after {confirm_days} consecutive days absent."
        if confirm_days
        else ""
    )
    tenure_codes = " or ".join(
        c for c in (_attr_code("start_date"), _attr_code("created")) if c
    )
    tenure_note = f" Tenure from {tenure_codes}." if tenure_codes else ""
    name_note = f" Uses {_attr_code('display_name')}." if "display_name" in ATTR else ""
    out = [
        f"<h3>{title}</h3>",
        "<p style='color:#666; font-size:12px; margin:-10px 0 15px 0;'>"
        f"{MEMBER_LABEL} no longer present in the directory.{confirm_text}"
        f"{name_note}{tenure_note}</p>",
        "<table><tr><th>Name</th><th>Username</th>",
    ]
    for role in detail_roles:
        out.append(f"<th>{html.escape(role_label(role))}</th>")
    out.append("<th>Tenure</th></tr>")

    has_flappers = False
    ordered = sorted(users, key=lambda r: _sort_key(r, homonym_names))
    shown, hidden = _cap_rows(ordered)
    for record in shown:
        is_flapper = record.get("dn", "") in flapper_dns
        has_flappers = has_flappers or is_flapper

        name = records.display_name(record)
        username = records.username(record)
        is_homonym = name in homonym_names
        name_html = _name_with_badge(name, username, is_homonym)
        if is_flapper:
            name_html += " *"

        start = parse_join_date(
            records.value(record, "start_date"), records.value(record, "created")
        )
        tenure = tenure_str(start, now) if start else "Unknown"

        out.append(f"<tr{_row_style(is_homonym)}>")
        out.append(f"<td>{name_html}</td><td>{html.escape(username)}</td>")
        for role in detail_roles:
            attribute = ATTR.get(role, role)
            value = smart_clean_val(attribute, records.value(record, role))
            out.append(f"<td>{html.escape(value)}</td>")
        out.append(f"<td>{tenure}</td></tr>")
    out.append("</table>")
    out.append(_truncation_note(hidden, len(users)))

    if has_flappers:
        out.append(
            "<p style='color:#888; font-size:11px; margin-top:5px;'>"
            "* Has previously dropped out of the directory and returned — this "
            "may not be a permanent departure.</p>"
        )
    out.append(
        _homonym_footnote([records.display_name(u) for u in users], homonym_names)
    )
    return "".join(out)


def build_seniority_table(all_users, top_n=None, as_of=None):
    """Render the longest-tenured table, or '' when no dates are available.

    Sorted by the earliest available start date.  Records with no parseable
    date sort last rather than being dropped, so the count stays honest.

    ``as_of`` anchors the tenure column, so a replayed report shows the
    tenure people had on that date.
    """
    top_n = TOP_N.get("seniority", 100) if top_n is None else top_n
    if top_n <= 0 or not all_users:
        return ""
    if "start_date" not in ATTR and "created" not in ATTR:
        return ""

    def start_of(record):
        return parse_join_date(
            records.value(record, "start_date"), records.value(record, "created")
        )

    dated = [(start_of(r), r) for r in all_users]
    if not any(start for start, _ in dated):
        return ""

    dated.sort(key=lambda pair: pair[0] or datetime.max)
    now = as_of or datetime.now()
    detail_roles = [r for r in ("job_title", "department", "country") if r in ATTR]

    heading = f"{ORG_NAME} Seniority" if ORG_NAME else "Longest Tenured"
    out = [
        f"<div style='margin-top:40px;'><h3>🏆 {html.escape(heading)}</h3>",
        "<p style='color:#666; font-size:12px; margin:-10px 0 15px 0;'>"
        f"Top {top_n} longest-tenured {MEMBER_LABEL.lower()}.</p>",
        "<table><tr><th>Rank</th><th>Name</th>",
    ]
    for role in detail_roles:
        out.append(f"<th>{html.escape(role_label(role))}</th>")
    out.append("<th>Joined</th><th>Tenure</th></tr>")

    for rank, (start, record) in enumerate(dated[:top_n], 1):
        joined = start.strftime("%Y-%m-%d") if start else "Unknown"
        tenure = tenure_str(start, now) if start else "Unknown"
        out.append(
            f"<tr><td>{rank}</td>"
            f"<td><b>{html.escape(records.display_name(record))}</b></td>"
        )
        for role in detail_roles:
            attribute = ATTR.get(role, role)
            value = smart_clean_val(attribute, records.value(record, role))
            out.append(f"<td>{html.escape(value)}</td>")
        out.append(f"<td>{joined}</td><td>{tenure}</td></tr>")
    out.append("</table></div>")
    return "".join(out)


# ──────────────────────────────────────────────
# Document assembly
# ──────────────────────────────────────────────

_STYLE = """
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; font-size: 14px; color: #333; }
            .container { border: 1px solid #ddd; border-radius: 6px; max-width: 1200px; margin: 0 auto; background-color: #fff; }
            .section { padding: 25px; }
            h3 { border-bottom: 2px solid #eee; padding-bottom: 8px; color: #444; margin-top: 40px; margin-bottom: 15px; }
            table { width: 100%; border-collapse: collapse; font-size: 13px; }
            th { text-align: left; background-color: #f4f4f4; padding: 10px; border-bottom: 2px solid #ddd; font-weight: 600; }
            td { padding: 10px; border-bottom: 1px solid #eee; vertical-align: top; }
            tr:hover td { background-color: #f9f9f9; }
            .badge { background-color: #eee; padding: 2px 6px; border-radius: 3px; font-size: 11px; color: #555; font-family: monospace; }
            .metric-box { display: inline-block; margin-right: 30px; text-align: center; min-width: 80px; }
            .metric-val { font-size: 24px; font-weight: bold; display: block; margin-bottom: 4px; }
            .metric-lbl { font-size: 11px; color: #777; text-transform: uppercase; letter-spacing: 0.5px; }
            .footer { font-size: 10px; color: #999; text-align: center; margin-top: 40px; padding-bottom: 10px; }
            .graph-container { text-align: center; border: 1px solid #eee; padding: 15px; margin-bottom: 25px; border-radius: 4px; }
"""


def _format_report_date(report_date):
    """Return a long-form date string without a platform-specific format code.

    ``%-d`` is a glibc/BSD extension that raises on Windows, so the day is
    interpolated rather than formatted.
    """
    return f"{report_date.strftime('%A, %B')} {report_date.day}, {report_date.year}"


def build_report_html(
    new_users,
    leavers,
    modifications,
    conn,
    today_str,
    current_state,
    window_days=1,
    leaver_window_days=None,
    flapper_dns=None,
    has_trend=False,
):
    """Assemble the complete HTML document for a run.

    Separated from delivery so it can be rendered and asserted on in tests
    without SMTP, a mail server, or a live directory anywhere in the picture.
    """
    total_count = len(current_state)
    leaver_window_days = (
        window_days if leaver_window_days is None else leaver_window_days
    )
    flapper_dns = flapper_dns or {}

    window_short = window_label_short(window_days)
    window_long = window_label(window_days)
    leaver_short = window_label_short(leaver_window_days)

    try:
        homonym_names = find_homonym_names(new_users, leavers, modifications)
    except Exception as e:
        logging.error("Homonym detection failed: %s", e)
        homonym_names = set()

    highlights_html = ""
    if HIGHLIGHTS_ENABLED:
        highlights_html = build_highlights_strip(conn, today_str, leavers)

    graph_html = ""
    if has_trend:
        graph_html = (
            "<div class='graph-container'>"
            "<img src='cid:trend_graph' style='width:100%; max-width:1000px;' "
            "alt='Headcount trend'></div>"
        )

    joiner_roles = [r for r in ("job_title", "department", "country") if r in ATTR]
    name_note = (
        f" Uses {_attr_code('display_name')} (preferred name)."
        if "display_name" in ATTR
        else ""
    )
    # Tenure is computed as of the end of the report's day, not the wall
    # clock: on the nightly run the two agree to the day, and on a --replay
    # of a past date the wall clock would silently inflate every tenure.
    as_of = datetime.strptime(today_str, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59
    )

    joiners_html = build_joiner_table(
        f"✅ New Joiners ({window_short})",
        new_users,
        joiner_roles,
        f"{MEMBER_LABEL} that appeared in the directory in the "
        f"{window_long}.{name_note}",
        homonym_names=homonym_names,
    )
    leavers_html = build_leaver_table(
        f"❌ Leavers ({leaver_short})",
        leavers,
        flapper_dns=flapper_dns,
        confirm_days=leaver_window_days,
        homonym_names=homonym_names,
        as_of=as_of,
    )
    changes_html = render_attribute_changes(
        modifications, BULK_RENAME_MIN, window_long, homonym_names
    )
    aggregates_html = build_aggregate_sections(
        conn, today_str, total_count, current_state
    )
    seniority_html = build_seniority_table(list(current_state.values()), as_of=as_of)

    footer_link = ""
    if PROJECT_URL:
        safe_url = html.escape(PROJECT_URL, quote=True)
        footer_link = f"<br><a href='{safe_url}'>{html.escape(PROJECT_URL)}</a>"

    report_date = _format_report_date(datetime.strptime(today_str, "%Y-%m-%d"))

    return f"""
    <html>
    <head>
        <style>{_STYLE}</style>
    </head>
    <body>
        <div class="container">
        <div class="section">
            <div style="margin-bottom: 8px;">
                <span style="font-size: 18px; font-weight: 600; color: #333;">{report_date}</span>
            </div>
            <div style="margin-bottom: 30px; border-bottom: 1px solid #eee; padding-bottom: 20px;">
                <div class="metric-box">
                    <span class="metric-val">{total_count}</span>
                    <span class="metric-lbl">Active {html.escape(MEMBER_LABEL)}</span>
                </div>
                <div class="metric-box">
                    <span class="metric-val" style="color:green">+{len(new_users)}</span>
                    <span class="metric-lbl">New ({window_short})</span>
                </div>
                <div class="metric-box">
                    <span class="metric-val" style="color:red">-{len(leavers)}</span>
                    <span class="metric-lbl">Left ({leaver_short})</span>
                </div>
                <div class="metric-box">
                    <span class="metric-val" style="color:orange">{len(modifications)}</span>
                    <span class="metric-lbl">Changed ({window_short})</span>
                </div>
            </div>
            {highlights_html}
            {graph_html}
            {joiners_html}
            {leavers_html}
            {changes_html}
            {aggregates_html}
            {seniority_html}
            <div class="footer">Generated by LDAP Time Machine v{VERSION} | {today_str}{footer_link}</div>
        </div></div></body></html>
    """


def build_subject(new_users, leavers, modifications, window_short, leaver_short):
    """Return the email subject line for a run.

    A quiet day says so explicitly rather than showing three zeros, so it is
    obvious at a glance that the job ran and found nothing.
    """
    if not (new_users or leavers or modifications):
        return f"{SUBJECT_PREFIX}: Daily Summary (No Reportable Changes)"
    return (
        f"{SUBJECT_PREFIX}: {len(new_users)} New ({window_short}), "
        f"{len(leavers)} Left ({leaver_short}), "
        f"{len(modifications)} Changed ({window_short})"
    )


def build_message(subject, html_content, smtp, trend_path=None):
    """Build the MIME message, embedding the trend graph when present."""
    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = smtp.from_email
    msg["To"] = ", ".join(smtp.recipients)

    alternative = MIMEMultipart("alternative")
    msg.attach(alternative)
    alternative.attach(MIMEText(html_content, "html"))

    if trend_path and os.path.exists(trend_path):
        with open(trend_path, "rb") as f:
            image = MIMEImage(f.read())
        image.add_header("Content-ID", "<trend_graph>")
        msg.attach(image)
    return msg


def deliver(msg, smtp):
    """Send a message over SMTP, returning True on success.

    STARTTLS and authentication are applied only when configured, so an
    unauthenticated internal relay still works.  A delivery failure is logged
    and swallowed: the report is already archived on disk, and raising here
    would turn a mail problem into a failed run.
    """
    try:
        logging.info("Sending email to %s...", smtp.recipients)
        with smtplib.SMTP(smtp.server, smtp.port, timeout=SMTP_TIMEOUT_SECONDS) as s:
            if smtp.use_tls:
                s.starttls()
                s.ehlo()
            if smtp.username and smtp.password:
                s.login(smtp.username, smtp.password)
            s.sendmail(smtp.from_email, smtp.recipients, msg.as_string())
        logging.info("Email sent successfully: %s", msg["Subject"])
        return True
    except Exception as e:
        logging.error("Email send failed: %s", e)
        return False


def send_email_report(
    new_users,
    leavers,
    modifications,
    conn,
    today_str,
    current_state,
    window_days=1,
    leaver_window_days=None,
    flapper_dns=None,
    send_email=True,
):
    """Render the report, archive it, and optionally deliver it.

    The archive step happens before any SMTP work, so the rendered output
    survives even when delivery fails outright.

    Args:
        new_users: Joiner records.
        leavers: Confirmed leaver records.
        modifications: [{name, username, changes}].
        conn: An open sqlite3.Connection, for counts and the trend graph.
        today_str: Current date 'YYYY-MM-DD'.
        current_state: Today's full {dn: record} snapshot.
        window_days: Window for the joiners and changes headings.
        leaver_window_days: Window for the leavers heading.
        flapper_dns: {dn: flap_count} for known flappers.
        send_email: When False, archive but do not deliver.

    Returns:
        The path of the archived HTML, or None when archiving failed.
    """
    leaver_window_days = (
        window_days if leaver_window_days is None else leaver_window_days
    )
    logging.info("Generating report for %d records...", len(current_state))

    has_trend = False
    try:
        has_trend = generate_trend_graph(conn, end_date_str=today_str)
    except Exception as e:
        logging.error("Trend graph generation failed: %s", e)
    if not has_trend:
        logging.warning("No trend graph in this report.")

    html_content = build_report_html(
        new_users,
        leavers,
        modifications,
        conn,
        today_str,
        current_state,
        window_days=window_days,
        leaver_window_days=leaver_window_days,
        flapper_dns=flapper_dns,
        has_trend=has_trend,
    )

    archive_path = archive_email_html(html_content, dry_run=not send_email)

    smtp = get_smtp_settings()
    if not send_email or not smtp.enabled:
        logging.info("Email delivery is off for this run; skipping SMTP.")
        return archive_path

    subject = build_subject(
        new_users,
        leavers,
        modifications,
        window_label_short(window_days),
        window_label_short(leaver_window_days),
    )
    msg = build_message(
        subject, html_content, smtp, TREND_GRAPH_FILE if has_trend else None
    )
    deliver(msg, smtp)
    return archive_path


def archive_email_html(html_content, dry_run=False, directory=None):
    """Write the rendered report to disk and prune old archives.

    Real reports and dry-run reports are retained in separate buckets, so
    testing a change cannot evict the production reports you might need to
    compare against.  Pruning is by modification time, oldest first.

    Failures are logged and swallowed — a full disk should not cost you the
    email that already rendered successfully.
    """
    directory = directory or EMAIL_ARCHIVE_DIR
    try:
        os.makedirs(directory, exist_ok=True)
        prefix = "dryrun_" if dry_run else "ldap_report_"
        keep = ARCHIVE_KEEP_DRY_RUN if dry_run else ARCHIVE_KEEP
        # Microseconds keep the name unique when two archives land within
        # the same second — a scripted `--replay` over a list of dates does
        # exactly that, and the second file must not overwrite the first.
        filename = f"{prefix}{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.html"
        path = os.path.join(directory, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html_content)
        logging.info("Archived report: %s", path)

        existing = [
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if name.startswith(prefix)
            and name.endswith(".html")
            and os.path.isfile(os.path.join(directory, name))
        ]
        if len(existing) <= keep:
            return path
        existing.sort(key=os.path.getmtime)
        for old_path in existing[:-keep]:
            os.remove(old_path)
        return path
    except Exception as e:
        logging.error("Failed to archive report: %s", e)
        return None
