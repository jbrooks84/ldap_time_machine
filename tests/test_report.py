"""Tests for report rendering, assembly, and delivery.

The escaping tests are the ones that matter most: every value in the report
came from a directory, and a display name containing markup would otherwise be
rendered as markup in everyone's mail client.
"""

import os
import re
from typing import ClassVar

import pytest

from ltm import report
from ltm.config import ATTR
from ltm.secrets import SmtpSettings

from .conftest import ATTR_DEPT, ATTR_DISPLAY, ATTR_TITLE, dn_for, make_record


def strip_tags(html_text):
    """Return the visible text of an HTML fragment."""
    return re.sub(r"<[^>]+>", "", html_text)


# ── Role labels ───────────────────────────────────────────────────


def test_role_label_uses_the_lookup_table():
    assert report.role_label("job_title") == "Title"
    assert report.role_label("business_category") == "Business Category"


def test_role_label_falls_back_to_a_derived_name():
    assert report.role_label("some_new_role") == "Some New Role"


def test_role_emoji_has_a_default():
    assert report.role_emoji("job_title") == "👔"
    assert report.role_emoji("unmapped_role") == "🔁"


def test_attr_code_renders_the_directory_attribute():
    assert report._attr_code("job_title") == f"<code>{ATTR.job_title}</code>"


def test_attr_code_is_empty_for_an_unmapped_role():
    assert report._attr_code("start_date") == ""


# ── Homonyms ──────────────────────────────────────────────────────


def test_no_duplicates_yields_no_homonyms():
    users = [make_record("a", "Alice"), make_record("b", "Bob")]
    assert report.find_homonym_names(users, [], []) == set()


def test_duplicate_within_one_list_is_detected():
    users = [make_record("a", "Alice"), make_record("a2", "Alice")]
    assert report.find_homonym_names(users, [], []) == {"Alice"}


def test_duplicate_across_joiners_and_leavers_is_detected():
    """The common case: one namesake arrives as another leaves."""
    joiners = [make_record("a", "Alice")]
    leavers = [make_record("a2", "Alice")]
    assert report.find_homonym_names(joiners, leavers, []) == {"Alice"}


def test_duplicate_between_a_record_and_a_modification_is_detected():
    joiners = [make_record("a", "Alice")]
    mods = [{"name": "Alice", "username": "a2", "changes": []}]
    assert report.find_homonym_names(joiners, [], mods) == {"Alice"}


def test_homonym_detection_ignores_blank_names():
    assert report.find_homonym_names([{}, {}], [], [{"name": "  "}]) == set()


def test_homonym_detection_handles_none_inputs():
    assert report.find_homonym_names(None, None, None) == set()


def test_row_style_only_applies_to_homonyms():
    assert report._row_style(True) != ""
    assert report._row_style(False) == ""


def test_name_badge_appears_only_for_homonyms():
    assert "badge" in report._name_with_badge("Alice", "alice", True)
    assert "badge" not in report._name_with_badge("Alice", "alice", False)


def test_name_badge_is_omitted_without_a_username():
    assert "badge" not in report._name_with_badge("Alice", "", True)


def test_name_badge_escapes_both_values():
    out = report._name_with_badge("<script>", "<b>", True)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_footnote_appears_only_when_the_table_holds_a_homonym():
    assert report._homonym_footnote(["Alice"], {"Alice"}) != ""
    assert report._homonym_footnote(["Bob"], {"Alice"}) == ""
    assert report._homonym_footnote(["Alice"], set()) == ""


# ── Change splitting and grouping ─────────────────────────────────


def test_split_changes_maps_attributes_back_to_roles():
    mods = [
        {
            "name": "Alice",
            "username": "alice",
            "changes": [
                {"attr": ATTR.job_title, "old": "X", "new": "Y"},
                {"attr": ATTR.department, "old": "D1", "new": "D2"},
            ],
        }
    ]
    split = report.split_changes_by_role(mods)
    assert len(split["job_title"]) == 1
    assert split["job_title"][0]["name"] == "Alice"
    assert len(split["department"]) == 1


def test_split_changes_separates_country_from_city():
    mods = [
        {
            "name": "Carol",
            "username": "carol",
            "changes": [
                {"attr": ATTR.country, "old": "UK", "new": "IE"},
                {"attr": ATTR.city, "old": "London", "new": "Dublin"},
            ],
        }
    ]
    split = report.split_changes_by_role(mods)
    assert len(split["country"]) == 1
    assert len(split["city"]) == 1


def test_split_changes_drops_untracked_attributes():
    mods = [
        {
            "name": "A",
            "username": "a",
            "changes": [{"attr": "someRandomAttr", "old": "1", "new": "2"}],
        }
    ]
    split = report.split_changes_by_role(mods)
    assert all(not items for items in split.values())


def test_split_changes_handles_no_input():
    assert all(not v for v in report.split_changes_by_role(None).values())


def test_bulk_rename_groups_at_the_threshold():
    changes = [
        {"name": f"P{i}", "username": f"p{i}", "old": "Old", "new": "New"}
        for i in range(5)
    ] + [{"name": "X", "username": "x", "old": "A", "new": "B"}]
    bulk, individual = report.group_dept_changes(changes, threshold=3)
    assert len(bulk) == 1
    assert bulk[0]["count"] == 5
    assert len(individual) == 1


def test_below_the_threshold_stays_individual():
    changes = [
        {"name": "A", "username": "a", "old": "Foo", "new": "Bar"},
        {"name": "B", "username": "b", "old": "Foo", "new": "Bar"},
    ]
    bulk, individual = report.group_dept_changes(changes, threshold=3)
    assert bulk == []
    assert len(individual) == 2


def test_a_zero_threshold_disables_rollup_entirely():
    """The setting a clean directory uses: never hide an individual move."""
    changes = [
        {"name": f"P{i}", "username": f"p{i}", "old": "Old", "new": "New"}
        for i in range(50)
    ]
    bulk, individual = report.group_dept_changes(changes, threshold=0)
    assert bulk == []
    assert len(individual) == 50


def test_bulk_groups_are_sorted_largest_first():
    changes = [
        {"name": f"a{i}", "username": "", "old": "O1", "new": "N1"} for i in range(3)
    ] + [{"name": f"b{i}", "username": "", "old": "O2", "new": "N2"} for i in range(9)]
    bulk, _ = report.group_dept_changes(changes, threshold=3)
    assert [group["count"] for group in bulk] == [9, 3]


def test_bulk_members_are_sorted_case_insensitively():
    changes = [
        {"name": "Charlie", "username": "c", "old": "O", "new": "N"},
        {"name": "alice", "username": "a", "old": "O", "new": "N"},
        {"name": "Bob", "username": "b", "old": "O", "new": "N"},
    ]
    bulk, _ = report.group_dept_changes(changes, threshold=3)
    assert [m["name"] for m in bulk[0]["members"]] == ["alice", "Bob", "Charlie"]


def test_group_dept_changes_handles_no_input():
    assert report.group_dept_changes(None, 3) == ([], [])


def test_bulk_member_list_shows_everyone_under_the_limit():
    members = [{"name": n, "username": n.lower()} for n in ("Alice", "Bob", "Carol")]
    out = report.format_bulk_members(members, set(), preview_limit=10)
    assert out == "Alice, Bob, Carol"
    assert "more" not in out


def test_bulk_member_list_truncates_with_a_remainder_count():
    members = [{"name": f"P{i:03d}", "username": f"p{i}"} for i in range(226)]
    out = report.format_bulk_members(members, set(), preview_limit=10)
    assert "P000" in out and "P009" in out
    assert "P010" not in out
    assert "and 216 more" in out


def test_bulk_member_list_is_empty_without_members():
    assert report.format_bulk_members([], set(), 10) == ""


# ── Section renderers ─────────────────────────────────────────────


def test_role_change_section_renders_the_values():
    items = [
        {
            "name": "Alice",
            "username": "alice",
            "old": "Engineer",
            "new": "Staff Engineer",
        }
    ]
    out = report.render_role_changes("job_title", items, "last 24 hours")
    assert "Title Changes" in out
    assert "Engineer" in out and "Staff Engineer" in out


def test_role_change_section_is_empty_without_items():
    assert report.render_role_changes("job_title", [], "last 24 hours") == ""


def test_role_change_section_escapes_directory_values():
    items = [
        {
            "name": "<img src=x>",
            "username": "a",
            "old": "<script>alert(1)</script>",
            "new": "ok",
        }
    ]
    out = report.render_role_changes("job_title", items, "last 24 hours")
    assert "<script>" not in out
    assert "<img src=x>" not in out


def test_department_section_renders_both_subsections():
    items = [
        {"name": f"P{i}", "username": f"p{i}", "old": "Old", "new": "New"}
        for i in range(4)
    ] + [{"name": "Solo", "username": "s", "old": "A", "new": "B"}]
    out = report.render_dept_changes(items, 3, "last 24 hours")
    assert "Bulk renames" in out
    assert "Individual moves" in out
    assert "Solo" in out


def test_department_section_omits_the_rollup_note_when_disabled():
    items = [{"name": "Solo", "username": "s", "old": "A", "new": "B"}]
    out = report.render_dept_changes(items, 0, "last 24 hours")
    assert "rolled up" not in out


def test_department_section_is_empty_without_items():
    assert report.render_dept_changes([], 3, "last 24 hours") == ""


def test_location_section_merges_country_and_city_per_person():
    """One person who moved both must produce one row, not two."""
    country = [{"name": "Carol", "username": "carol", "old": "UK", "new": "IE"}]
    city = [{"name": "Carol", "username": "carol", "old": "London", "new": "Dublin"}]
    out = report.render_location_changes(country, city, "last 24 hours")
    assert out.count("<tr") == 2  # header row plus one data row
    assert "Dublin" in out and "IE" in out


def test_location_section_handles_only_one_of_the_two():
    country = [{"name": "Carol", "username": "carol", "old": "UK", "new": "IE"}]
    out = report.render_location_changes(country, [], "last 24 hours")
    assert "IE" in out


def test_location_section_is_empty_without_either():
    assert report.render_location_changes([], [], "last 24 hours") == ""


def test_attribute_changes_dispatch_renders_every_tracked_role():
    mods = [
        {
            "name": "Alice",
            "username": "alice",
            "changes": [
                {"attr": ATTR.job_title, "old": "E", "new": "SE"},
                {"attr": ATTR.department, "old": "D1", "new": "D2"},
                {"attr": ATTR.manager, "old": "CN=X,DC=e", "new": "CN=Y,DC=e"},
                {"attr": ATTR.country, "old": "UK", "new": "IE"},
            ],
        }
    ]
    out = report.render_attribute_changes(mods, 3, "last 24 hours")
    assert "Title Changes" in out
    assert "Department Changes" in out
    assert "Manager Changes" in out
    assert "Location Changes" in out


def test_manager_changes_show_names_not_dns():
    """A DN in a report is noise; the CN is the part a human can act on."""
    mods = [
        {
            "name": "Alice",
            "username": "alice",
            "changes": [
                {
                    "attr": ATTR.manager,
                    "old": "CN=Old Boss,OU=People,DC=example,DC=com",
                    "new": "CN=New Boss,OU=People,DC=example,DC=com",
                }
            ],
        }
    ]
    out = report.render_attribute_changes(mods, 3, "last 24 hours")
    assert "New Boss" in out
    assert "OU=People" not in out


def test_attribute_changes_isolates_a_failing_section(monkeypatch, caplog):
    """One broken renderer must not blank the others."""

    def boom(*_args, **_kwargs):
        raise RuntimeError("renderer exploded")

    monkeypatch.setattr(report, "render_dept_changes", boom)
    mods = [
        {
            "name": "Alice",
            "username": "alice",
            "changes": [
                {"attr": ATTR.job_title, "old": "E", "new": "SE"},
                {"attr": ATTR.department, "old": "D1", "new": "D2"},
            ],
        }
    ]
    with caplog.at_level("ERROR"):
        out = report.render_attribute_changes(mods, 3, "last 24 hours")
    assert "Title Changes" in out
    assert "Failed to render" in caplog.text


def test_attribute_changes_survives_a_failing_split(monkeypatch, caplog):
    def boom(*_args, **_kwargs):
        raise RuntimeError("split exploded")

    monkeypatch.setattr(report, "split_changes_by_role", boom)
    with caplog.at_level("ERROR"):
        assert report.render_attribute_changes([], 3, "last 24 hours") == ""
    assert "Attribute split failed" in caplog.text


def test_attribute_changes_with_nothing_to_show():
    assert report.render_attribute_changes([], 3, "last 24 hours") == ""


# ── Deltas and aggregate tables ───────────────────────────────────


def test_format_delta_covers_every_direction():
    assert "N/A" in report.format_delta(10, None)
    assert "+5" in report.format_delta(15, 10)
    assert "-5" in report.format_delta(5, 10)
    assert ">-<" in report.format_delta(10, 10)  # a dash, not a confusing zero


def test_aggregate_table_ranks_and_caps():
    items = {"A": 30, "B": 20, "C": 10}
    out = report.build_aggregate_table(
        "Top 2 Countries", "🌎", "desc", items, 60, None, None, None, top_n=2
    )
    assert "A" in out and "B" in out
    assert ">C<" not in out


def test_aggregate_table_computes_percentages():
    out = report.build_aggregate_table(
        "Top 5 Countries", "🌎", "desc", {"A": 25}, 100, None, None, None, top_n=5
    )
    assert "25.0%" in out


def test_aggregate_table_handles_a_zero_total():
    out = report.build_aggregate_table(
        "Top 5 Countries", "🌎", "desc", {"A": 0}, 0, None, None, None, top_n=5
    )
    assert "0.0%" in out


def test_aggregate_table_is_empty_when_disabled_or_empty():
    assert report.build_aggregate_table("T", "x", "d", {}, 1, None, None, None) == ""
    assert (
        report.build_aggregate_table(
            "T", "x", "d", {"A": 1}, 1, None, None, None, top_n=0
        )
        == ""
    )


def test_aggregate_table_escapes_values():
    out = report.build_aggregate_table(
        "Top 5 Countries", "🌎", "d", {"<script>": 1}, 1, None, None, None, top_n=5
    )
    assert "<script>" not in out


def test_singularise_handles_the_common_shapes():
    assert report._singularise("Countries") == "Country"
    assert report._singularise("Departments") == "Department"
    assert report._singularise("Staff") == "Staff"


def test_aggregate_sections_render_from_the_sample_database(
    sample_db, today, current_state
):
    out = report.build_aggregate_sections(
        sample_db, today, len(current_state), current_state
    )
    assert "Countries" in out
    assert "Departments" in out
    assert "United States" in out


def test_aggregate_sections_are_empty_with_no_enabled_tables(
    monkeypatch, sample_db, today
):
    monkeypatch.setattr(report, "TOP_N", {})
    assert report.build_aggregate_sections(sample_db, today, 10, {}) == ""


# ── People tables ─────────────────────────────────────────────────


def test_joiner_table_lists_people_and_columns():
    users = [make_record("dave", "Dave Diaz", title="Analyst", department="Finance")]
    out = report.build_joiner_table("New Joiners", users, ["job_title", "department"])
    assert "Dave Diaz" in out
    assert "Analyst" in out and "Finance" in out
    assert "Title" in out and "Department" in out


def test_joiner_table_is_empty_without_joiners():
    assert report.build_joiner_table("New Joiners", [], ["job_title"]) == ""


def test_joiner_table_sorts_homonyms_to_the_top():
    users = [
        make_record("zoe", "Zoe Zhang"),
        make_record("a1", "Alex Ambiguous"),
        make_record("a2", "Alex Ambiguous"),
    ]
    out = report.build_joiner_table(
        "New Joiners", users, ["job_title"], homonym_names={"Alex Ambiguous"}
    )
    assert out.index("Alex Ambiguous") < out.index("Zoe Zhang")


def test_joiner_table_escapes_a_hostile_display_name():
    users = [make_record("x", "<script>alert(1)</script>")]
    out = report.build_joiner_table("New Joiners", users, ["job_title"])
    assert "<script>" not in out


def test_leaver_table_shows_tenure_and_flapper_marks():
    leaver = make_record("frank", "Frank Foster", created="20180601090000.0Z")
    leaver["dn"] = dn_for("frank")
    out = report.build_leaver_table(
        "Leavers", [leaver], flapper_dns={dn_for("frank"): 2}, confirm_days=7
    )
    assert "Frank Foster *" in strip_tags(out)
    assert "previously dropped out" in out
    assert "Confirmed after 7 consecutive days absent" in out


def test_leaver_table_omits_the_flapper_note_when_there_are_none():
    leaver = make_record("erin", "Erin Evans")
    leaver["dn"] = dn_for("erin")
    out = report.build_leaver_table("Leavers", [leaver])
    assert "previously dropped out" not in out


def test_leaver_table_reports_unknown_tenure_without_a_date():
    leaver = make_record("x", "No Dates")
    del leaver["whenCreated"]
    out = report.build_leaver_table("Leavers", [leaver])
    assert "Unknown" in out


def test_leaver_table_is_empty_without_leavers():
    assert report.build_leaver_table("Leavers", []) == ""


def test_seniority_table_ranks_by_earliest_start(current_state):
    out = report.build_seniority_table(list(current_state.values()))
    # Carol has the earliest creation date in the fixture.
    assert "Carol Chen" in out
    assert out.index("Carol Chen") < out.index("Alice Adams")


def test_seniority_table_respects_the_row_cap(current_state):
    out = report.build_seniority_table(list(current_state.values()), top_n=2)
    assert strip_tags(out).count("Core Platform") <= 2


def test_seniority_table_is_empty_when_disabled_or_dateless():
    users = [make_record("a", "Alice")]
    assert report.build_seniority_table(users, top_n=0) == ""
    assert report.build_seniority_table([], top_n=10) == ""

    dateless = [{ATTR_DISPLAY: "No Date"}]
    assert report.build_seniority_table(dateless, top_n=10) == ""


def test_seniority_heading_uses_the_configured_org_name(monkeypatch, current_state):
    monkeypatch.setattr(report, "ORG_NAME", "Acme")
    out = report.build_seniority_table(list(current_state.values()))
    assert "Acme Seniority" in out


def test_seniority_heading_is_neutral_without_an_org_name(monkeypatch, current_state):
    monkeypatch.setattr(report, "ORG_NAME", "")
    out = report.build_seniority_table(list(current_state.values()))
    assert "Longest Tenured" in out


# ── Highlights strip ──────────────────────────────────────────────


def test_highlights_strip_renders_from_the_sample_database(sample_db, today):
    out = report.build_highlights_strip(sample_db, today, [])
    assert "Highlights" in out
    assert "Net headcount" in out


def test_highlights_strip_reports_zero_leavers_positively(sample_db, today):
    out = report.build_highlights_strip(sample_db, today, [])
    assert "No confirmed leavers" in out


def test_highlights_render_placeholders_for_missing_tiles():
    out = report.render_highlights_html(None, None, None, None)
    assert "—" in out


def test_highlights_render_headcount_deltas():
    out = report.render_highlights_html(
        None, None, None, {"now": 1234, "delta_30": 10, "delta_90": -5}
    )
    assert "1,234" in out
    assert "+10 (30d)" in out
    assert "-5 (90d)" in out


def test_highlights_render_a_mover_tile():
    tile = {
        "country": "Japan",
        "past": 100,
        "now": 120,
        "delta": 20,
        "pct": 20.0,
        "days_label": "30d",
    }
    out = report.render_highlights_html(None, tile, None, None)
    assert "Japan" in out
    assert "+20%" in out


def test_highlights_strip_survives_a_failing_tile(
    monkeypatch, sample_db, today, caplog
):
    def boom(*_args, **_kwargs):
        raise RuntimeError("tile exploded")

    monkeypatch.setattr(report, "headcount_deltas", boom)
    with caplog.at_level("ERROR"):
        out = report.build_highlights_strip(sample_db, today, [])
    assert "Highlights" in out  # The other three tiles still rendered.
    assert "Highlight tile" in caplog.text


def test_highlights_strip_returns_empty_on_a_total_failure(caplog):
    with caplog.at_level("ERROR"):
        out = report.build_highlights_strip(None, "not-a-date", [])
    assert out == ""
    assert "build_highlights_strip failed" in caplog.text


def test_safe_call_returns_none_on_failure(caplog):
    def boom():
        raise RuntimeError("nope")

    with caplog.at_level("ERROR"):
        assert report._safe_call(boom) is None


def test_signed_formats_both_directions():
    assert report._signed(5) == "+5"
    assert report._signed(-5) == "-5"
    assert report._signed(0) == "+0"


# ── Trend graph ───────────────────────────────────────────────────


def test_trend_graph_is_written(sample_db, tmp_path):
    path = tmp_path / "trend.png"
    assert report.generate_trend_graph(sample_db, path=str(path)) is True
    assert path.exists() and path.stat().st_size > 0


def test_trend_graph_is_skipped_with_no_history(conn, tmp_path):
    path = tmp_path / "trend.png"
    assert report.generate_trend_graph(conn, path=str(path)) is False
    assert not path.exists()


def test_trend_graph_handles_a_single_flat_data_point(conn, tmp_path):
    """Padding derived from the data range must not collapse to zero."""
    conn.execute("INSERT INTO user_history VALUES ('2026-01-01', 'CN=A', '{}', '')")
    path = tmp_path / "trend.png"
    assert report.generate_trend_graph(conn, path=str(path)) is True


# ── Document assembly ─────────────────────────────────────────────


def test_report_date_avoids_platform_specific_format_codes():
    from datetime import datetime

    assert report._format_report_date(datetime(2026, 6, 30)) == "Tuesday, June 30, 2026"


def test_full_report_contains_every_section(sample_db, today, current_state):
    leaver = make_record("erin", "Erin Evans")
    leaver["dn"] = dn_for("erin")
    mods = [
        {
            "name": "Alice Adams",
            "username": "alice",
            "changes": [{"attr": ATTR.job_title, "old": "E", "new": "SE"}],
        }
    ]
    html_text = report.build_report_html(
        [make_record("dave", "Dave Diaz")],
        [leaver],
        mods,
        sample_db,
        today,
        current_state,
    )
    assert "New Joiners" in html_text
    assert "Leavers" in html_text
    assert "Title Changes" in html_text
    assert "Countries" in html_text
    assert "Tuesday, June 30, 2026" in html_text
    assert html_text.strip().endswith("</html>")


def test_full_report_renders_with_nothing_to_say(sample_db, today, current_state):
    """A quiet day must still produce a valid document."""
    html_text = report.build_report_html([], [], [], sample_db, today, current_state)
    assert "</html>" in html_text
    assert "New Joiners" not in html_text


def test_report_includes_the_graph_only_when_one_exists(
    sample_db, today, current_state
):
    with_graph = report.build_report_html(
        [], [], [], sample_db, today, current_state, has_trend=True
    )
    without = report.build_report_html(
        [], [], [], sample_db, today, current_state, has_trend=False
    )
    assert "cid:trend_graph" in with_graph
    assert "cid:trend_graph" not in without


def test_report_includes_the_highlights_strip_only_when_enabled(
    monkeypatch, sample_db, today, current_state
):
    monkeypatch.setattr(report, "HIGHLIGHTS_ENABLED", False)
    assert "Highlights" not in report.build_report_html(
        [], [], [], sample_db, today, current_state
    )
    monkeypatch.setattr(report, "HIGHLIGHTS_ENABLED", True)
    assert "Highlights" in report.build_report_html(
        [], [], [], sample_db, today, current_state
    )


def test_report_footer_links_only_when_a_url_is_configured(
    monkeypatch, sample_db, today, current_state
):
    monkeypatch.setattr(report, "PROJECT_URL", "")
    assert "<a href" not in report.build_report_html(
        [], [], [], sample_db, today, current_state
    )
    monkeypatch.setattr(report, "PROJECT_URL", "https://example.com/repo")
    assert "https://example.com/repo" in report.build_report_html(
        [], [], [], sample_db, today, current_state
    )


def test_report_survives_a_failing_homonym_pass(
    monkeypatch, sample_db, today, current_state, caplog
):
    def boom(*_args, **_kwargs):
        raise RuntimeError("homonyms exploded")

    monkeypatch.setattr(report, "find_homonym_names", boom)
    with caplog.at_level("ERROR"):
        html_text = report.build_report_html(
            [], [], [], sample_db, today, current_state
        )
    assert "</html>" in html_text
    assert "Homonym detection failed" in caplog.text


def test_report_contains_no_unescaped_directory_input(sample_db, today):
    """End-to-end escaping check across every section at once."""
    hostile = make_record(
        "evil",
        "<script>alert('xss')</script>",
        title="<img src=x onerror=alert(1)>",
        department="</table><h1>injected",
    )
    state = {dn_for("evil"): hostile}
    hostile_with_dn = dict(hostile, dn=dn_for("evil"))
    mods = [
        {
            "name": hostile[ATTR_DISPLAY],
            "username": "evil",
            "changes": [{"attr": ATTR.job_title, "old": "<b>", "new": "<i>"}],
        }
    ]
    html_text = report.build_report_html(
        [hostile], [hostile_with_dn], mods, sample_db, today, state
    )
    # The check that matters is that no directory value survives as live
    # markup. The escaped text may still read "onerror=" — inert, because the
    # surrounding angle brackets became entities and a browser sees only
    # characters. So assert on the exact injected payloads, each of which is
    # markup the report itself never emits.
    for payload in (
        "<script>alert",
        "<img src=x",
        "<h1>injected",
    ):
        assert payload not in html_text
    # ...and prove the values were rendered at all, rather than dropped.
    assert "&lt;script&gt;" in html_text


# ── Subject lines ─────────────────────────────────────────────────


def test_subject_summarises_a_busy_day():
    subject = report.build_subject([1, 2], [3], [4, 5, 6], "24h", "7d")
    assert "2 New (24h)" in subject
    assert "1 Left (7d)" in subject
    assert "3 Changed (24h)" in subject


def test_subject_says_so_on_a_quiet_day():
    """Three zeros reads as broken; say explicitly that nothing happened."""
    assert "No Reportable Changes" in report.build_subject([], [], [], "24h", "7d")


def test_subject_uses_the_configured_prefix(monkeypatch):
    monkeypatch.setattr(report, "SUBJECT_PREFIX", "Acme Directory")
    assert report.build_subject([], [], [], "24h", "7d").startswith("Acme Directory:")


# ── Message construction and delivery ─────────────────────────────


def smtp_settings(**overrides):
    """Build an SmtpSettings tuple with sensible test defaults."""
    defaults = {
        "server": "smtp.test",
        "port": 25,
        "from_email": "from@test",
        "recipients": ["to@test"],
        "use_tls": False,
        "username": None,
        "password": None,
        "enabled": True,
    }
    defaults.update(overrides)
    return SmtpSettings(**defaults)


def test_message_carries_the_headers_and_body():
    msg = report.build_message("Subject line", "<p>body</p>", smtp_settings())
    assert msg["Subject"] == "Subject line"
    assert msg["From"] == "from@test"
    assert msg["To"] == "to@test"
    assert "body" in msg.as_string()


def test_message_joins_multiple_recipients():
    msg = report.build_message(
        "S", "<p>b</p>", smtp_settings(recipients=["a@test", "b@test"])
    )
    assert msg["To"] == "a@test, b@test"


def test_message_embeds_the_trend_graph(tmp_path):
    image = tmp_path / "trend.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    msg = report.build_message("S", "<p>b</p>", smtp_settings(), str(image))
    assert "trend_graph" in msg.as_string()


def test_message_skips_a_missing_trend_graph(tmp_path):
    msg = report.build_message(
        "S", "<p>b</p>", smtp_settings(), str(tmp_path / "absent.png")
    )
    assert "trend_graph" not in msg.as_string()


class FakeSMTP:
    """Records what a delivery attempt did, without touching the network."""

    instances: ClassVar[list] = []

    def __init__(self, server, port, timeout=None):
        self.server = server
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.logged_in_as = None
        self.sent = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def starttls(self):
        self.started_tls = True

    def ehlo(self):
        pass

    def login(self, username, password):
        self.logged_in_as = (username, password)

    def sendmail(self, sender, recipients, body):
        self.sent = (sender, recipients, body)


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(report.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def test_deliver_sends_over_a_plain_relay(fake_smtp):
    msg = report.build_message("S", "<p>b</p>", smtp_settings())
    assert report.deliver(msg, smtp_settings()) is True
    sent = fake_smtp.instances[0]
    assert sent.sent[0] == "from@test"
    assert sent.started_tls is False
    assert sent.logged_in_as is None


def test_deliver_negotiates_starttls_when_configured(fake_smtp):
    settings = smtp_settings(use_tls=True)
    report.deliver(report.build_message("S", "b", settings), settings)
    assert fake_smtp.instances[0].started_tls is True


def test_deliver_authenticates_when_credentials_are_present(fake_smtp):
    settings = smtp_settings(username="user", password="pass")
    report.deliver(report.build_message("S", "b", settings), settings)
    assert fake_smtp.instances[0].logged_in_as == ("user", "pass")


def test_deliver_reports_failure_without_raising(monkeypatch, caplog):
    """A mail problem must not turn into a failed run."""

    def boom(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(report.smtplib, "SMTP", boom)
    settings = smtp_settings()
    with caplog.at_level("ERROR"):
        assert report.deliver(report.build_message("S", "b", settings), settings) is (
            False
        )
    assert "Email send failed" in caplog.text


# ── Archiving ─────────────────────────────────────────────────────


def test_archive_writes_the_file(tmp_path):
    path = report.archive_email_html("<html>x</html>", directory=str(tmp_path))
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as handle:
        assert handle.read() == "<html>x</html>"


def test_archive_uses_separate_prefixes_for_dry_runs(tmp_path):
    real = report.archive_email_html("<html>a</html>", directory=str(tmp_path))
    dry = report.archive_email_html(
        "<html>b</html>", dry_run=True, directory=str(tmp_path)
    )
    assert os.path.basename(real).startswith("ldap_report_")
    assert os.path.basename(dry).startswith("dryrun_")


def test_archive_prunes_the_oldest_beyond_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "ARCHIVE_KEEP", 3)
    for index in range(6):
        path = tmp_path / f"ldap_report_2026010{index}_000000.html"
        path.write_text("old", encoding="utf-8")
        os.utime(path, (index, index))
    report.archive_email_html("<html>new</html>", directory=str(tmp_path))
    remaining = sorted(p.name for p in tmp_path.glob("ldap_report_*.html"))
    assert len(remaining) == 3


def test_archive_pruning_does_not_cross_buckets(tmp_path, monkeypatch):
    """Testing a change must never evict the real reports."""
    monkeypatch.setattr(report, "ARCHIVE_KEEP_DRY_RUN", 1)
    real = tmp_path / "ldap_report_20260101_000000.html"
    real.write_text("keep me", encoding="utf-8")
    for _ in range(3):
        report.archive_email_html(
            "<html>d</html>", dry_run=True, directory=str(tmp_path)
        )
    assert real.exists()


def test_archive_reports_failure_without_raising(caplog):
    with caplog.at_level("ERROR"):
        assert report.archive_email_html("<html/>", directory="/proc/nope") is None
    assert "Failed to archive report" in caplog.text


# ── Top-level entry point ─────────────────────────────────────────


def test_send_email_report_archives_without_sending(
    sample_db, today, current_state, isolated_paths, fake_smtp
):
    report.send_email_report(
        [], [], [], sample_db, today, current_state, send_email=False
    )
    archived = list(isolated_paths["archive"].glob("dryrun_*.html"))
    assert len(archived) == 1
    assert fake_smtp.instances == []


def test_send_email_report_delivers_when_enabled(
    sample_db, today, current_state, isolated_paths, fake_smtp, monkeypatch
):
    monkeypatch.setattr(report, "get_smtp_settings", smtp_settings)
    report.send_email_report(
        [make_record("dave", "Dave Diaz")],
        [],
        [],
        sample_db,
        today,
        current_state,
        send_email=True,
    )
    assert len(fake_smtp.instances) == 1
    assert list(isolated_paths["archive"].glob("ldap_report_*.html"))


def test_send_email_report_skips_delivery_when_smtp_is_disabled(
    sample_db, today, current_state, isolated_paths, fake_smtp, monkeypatch
):
    monkeypatch.setattr(
        report, "get_smtp_settings", lambda: smtp_settings(enabled=False)
    )
    report.send_email_report(
        [], [], [], sample_db, today, current_state, send_email=True
    )
    assert fake_smtp.instances == []
    # The report is still on disk even though nothing was sent.
    assert list(isolated_paths["archive"].glob("ldap_report_*.html"))


def test_send_email_report_survives_a_failing_trend_graph(
    monkeypatch, sample_db, today, current_state, isolated_paths, caplog
):
    def boom(*_args, **_kwargs):
        raise RuntimeError("matplotlib exploded")

    monkeypatch.setattr(report, "generate_trend_graph", boom)
    with caplog.at_level("ERROR"):
        report.send_email_report(
            [], [], [], sample_db, today, current_state, send_email=False
        )
    assert list(isolated_paths["archive"].glob("dryrun_*.html"))
    assert "Trend graph generation failed" in caplog.text


def test_send_email_report_uses_the_department_change_data(
    sample_db, today, current_state, isolated_paths
):
    """A bulk rename must reach the rendered document, not just the database."""
    mods = [
        {
            "name": f"Person {i}",
            "username": f"p{i}",
            "changes": [
                {"attr": ATTR.department, "old": "Platform", "new": "Core Platform"}
            ],
        }
        for i in range(5)
    ]
    report.send_email_report(
        [], [], mods, sample_db, today, current_state, send_email=False
    )
    archived = next(isolated_paths["archive"].glob("dryrun_*.html"))
    content = archived.read_text(encoding="utf-8")
    assert "Bulk renames" in content
    assert "Core Platform" in content


def test_change_rows_render_department_values(
    sample_db, today, current_state, isolated_paths
):
    mods = [
        {
            "name": "Solo Mover",
            "username": "solo",
            "changes": [{"attr": ATTR_DEPT, "old": "Alpha", "new": "Beta"}],
        }
    ]
    report.send_email_report(
        [], [], mods, sample_db, today, current_state, send_email=False
    )
    content = next(isolated_paths["archive"].glob("dryrun_*.html")).read_text(
        encoding="utf-8"
    )
    assert "Alpha" in content and "Beta" in content


def test_title_changes_reach_the_document(
    sample_db, today, current_state, isolated_paths
):
    mods = [
        {
            "name": "Bob Brown",
            "username": "bob",
            "changes": [
                {"attr": ATTR_TITLE, "old": "Engineer", "new": "Principal Engineer"}
            ],
        }
    ]
    report.send_email_report(
        [], [], mods, sample_db, today, current_state, send_email=False
    )
    content = next(isolated_paths["archive"].glob("dryrun_*.html")).read_text(
        encoding="utf-8"
    )
    assert "Principal Engineer" in content


# ── Table row caps ────────────────────────────────────────────────


def test_person_tables_cap_at_the_configured_maximum(monkeypatch):
    """The first run reports everyone as a joiner; uncapped at 50k people
    that measured 9 MB of HTML — larger than most relays accept."""
    monkeypatch.setattr(report, "MAX_TABLE_ROWS", 5)
    users = [make_record(f"u{i}", f"User {i:03d}") for i in range(12)]
    out = report.build_joiner_table("New Joiners", users, ["job_title"])
    assert out.count("<tr") == 1 + 5  # header + capped rows
    assert "Showing 5 of 12" in out
    assert "max_table_rows" in out


def test_the_row_cap_can_be_disabled(monkeypatch):
    monkeypatch.setattr(report, "MAX_TABLE_ROWS", 0)
    users = [make_record(f"u{i}", f"User {i:03d}") for i in range(12)]
    out = report.build_joiner_table("New Joiners", users, ["job_title"])
    assert out.count("<tr") == 1 + 12
    assert "Showing" not in out


def test_the_cap_keeps_homonyms_because_they_sort_first(monkeypatch):
    """The disambiguated rows are the ones a reader most needs; truncation
    must never be what hides them."""
    monkeypatch.setattr(report, "MAX_TABLE_ROWS", 3)
    users = [make_record(f"u{i}", f"Zed User {i:03d}") for i in range(6)]
    users += [make_record("twin1", "Ada Twin"), make_record("twin2", "Ada Twin")]
    out = report.build_joiner_table(
        "New Joiners", users, ["job_title"], homonym_names={"Ada Twin"}
    )
    assert out.count("Ada Twin") >= 2
    assert "Showing 3 of 8" in out


def test_change_tables_share_the_cap(monkeypatch):
    monkeypatch.setattr(report, "MAX_TABLE_ROWS", 3)
    items = [
        {"name": f"P{i}", "username": f"p{i}", "old": "A", "new": "B"}
        for i in range(10)
    ]
    out = report.render_role_changes("job_title", items, "last 24 hours")
    assert "Showing 3 of 10" in out


def test_leaver_and_location_tables_share_the_cap(monkeypatch):
    monkeypatch.setattr(report, "MAX_TABLE_ROWS", 2)
    leavers = [make_record(f"u{i}", f"User {i:03d}") for i in range(5)]
    assert "Showing 2 of 5" in report.build_leaver_table("Leavers", leavers)

    moved = [
        {"name": f"P{i}", "username": f"p{i}", "old": "UK", "new": "IE"}
        for i in range(5)
    ]
    out = report.render_location_changes(moved, [], "last 24 hours")
    assert "Showing 2 of 5" in out


def test_dept_individual_moves_share_the_cap(monkeypatch):
    monkeypatch.setattr(report, "MAX_TABLE_ROWS", 2)
    items = [
        {"name": f"P{i}", "username": f"p{i}", "old": f"D{i}", "new": f"E{i}"}
        for i in range(6)
    ]
    out = report.render_dept_changes(items, 99, "last 24 hours")
    assert "Showing 2 of 6" in out


# ── Aggregate source of truth ─────────────────────────────────────


def test_todays_aggregates_come_from_the_fetched_state(sample_db, today):
    """Today's counts must reflect what was fetched, not a stale DB read.

    The database holds one population; the state passed in names a country
    that exists nowhere in storage. The table must show the state's version.
    """
    state = {
        f"CN=F{i}": make_record(f"f{i}", f"Fresh {i:03d}", country="Freedonia")
        for i in range(10)
    }
    out = report.build_aggregate_sections(sample_db, today, 10, state)
    assert "Freedonia" in out


def test_aggregate_ties_break_alphabetically():
    """Tied counts must render in a stable order, whichever source counted
    them — the README promises reports can be diffed."""
    items = {"Zeta": 5, "Alpha": 5, "Mid": 7}
    out = report.build_aggregate_table(
        "Top 5 Things", "x", "d", items, 17, None, None, None, top_n=5
    )
    assert out.index("Mid") < out.index("Alpha") < out.index("Zeta")
