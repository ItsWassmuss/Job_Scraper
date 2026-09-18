"""Test LinkedIn card parsing and local relative-age filtering.

Uses 1 real LinkedIn card (kept for reference to verify parsing against
actual markup) plus 4 synthetic cards testing edge cases (salary, plain-text
company, empty location, non-job card). Filtering is applied by callers
(role_is_relevant).
"""
import pytest

import scrape_jobs
from scrape_jobs import (
    _linkedin_relative_age_seconds,
    _linkedin_search,
    _parse_linkedin_cards,
)


def test_parses_raw_cards(linkedin_search_results_html):
    """Fixture should contain 4 job cards (1 real + 3 synthetic with URNs)."""
    jobs, raw_count = _parse_linkedin_cards(linkedin_search_results_html)
    assert raw_count == 4
    assert len(jobs) == 4  # all cards with URNs parsed, no filtering at this layer


def test_parsed_jobs_have_required_fields(linkedin_search_results_html):
    """Each parsed job must have id, company, title, location, date_posted."""
    jobs, _ = _parse_linkedin_cards(linkedin_search_results_html)
    assert len(jobs) > 0
    for job in jobs:
        assert "id" in job
        assert "company" in job
        assert "title" in job
        assert "location" in job
        assert "date_posted" in job


def test_real_card_relative_age_and_existing_fields(linkedin_search_results_html):
    jobs, _ = _parse_linkedin_cards(linkedin_search_results_html)
    real_card = next(job for job in jobs if job["id"] == "4434160560")

    assert real_card["id"] == "4434160560"
    assert real_card["company"] == "HiveWatch"
    assert real_card["title"] == "Director of Engineering"
    assert real_card["location"] == "Remote, CA"
    assert real_card["date_posted"] == "2026-08-14"
    assert real_card["_relative_age_text"] == "2 days ago"


def test_company_parsed_for_all_cards(linkedin_search_results_html):
    """Company must be parsed for every card (needed by role_is_relevant)."""
    jobs, _ = _parse_linkedin_cards(linkedin_search_results_html)
    companies = [j["company"] for j in jobs]
    assert any(c != "Unknown" for c in companies), (
        "All companies are 'Unknown' — company may not be parsed correctly"
    )


def test_empty_html():
    """Empty HTML should return ([], 0)."""
    jobs, raw_count = _parse_linkedin_cards("")
    assert jobs == []
    assert raw_count == 0


def test_non_job_card_skipped():
    """HTML with a <li> that has no jobPosting URN should be skipped."""
    html = """
    <li>
      <div class="some-other-card">Not a job card</div>
    </li>
    """
    jobs, raw_count = _parse_linkedin_cards(html)
    assert jobs == []
    assert raw_count == 0


def test_single_card_parsed():
    """A single valid card should be parsed with all fields."""
    html = """
    <li>
      <div class="base-card" data-entity-urn="urn:li:jobPosting:123">
        <a class="base-card__full-link" href="/jobs/view/123/">
          <span class="sr-only">Software Engineer at TechCorp</span>
        </a>
        <h3 class="base-search-card__title">Software Engineer</h3>
        <h4 class="base-search-card__subtitle">
          <a>TechCorp</a>
        </h4>
        <span class="job-search-card__location">San Francisco, CA</span>
        <time datetime="2026-08-14"></time>
      </div>
    </li>
    """
    jobs, raw_count = _parse_linkedin_cards(html)
    assert raw_count == 1
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Software Engineer"
    assert jobs[0]["company"] == "TechCorp"
    assert jobs[0]["location"] == "San Francisco, CA"
    assert jobs[0]["date_posted"] == "2026-08-14"
    assert jobs[0]["_relative_age_text"] == ""


@pytest.mark.parametrize(
    ("visible_time", "expected"),
    [
        ("5 minutes ago", "5 minutes ago"),
        ("6 hours ago", "6 hours ago"),
        ("7 hours ago", "7 hours ago"),
        ("1 day ago", "1 day ago"),
        ("Reposted <span>3</span>&nbsp; hours ago", "Reposted 3 hours ago"),
        ("", ""),
        ("Actively recruiting", "Actively recruiting"),
    ],
)
def test_visible_relative_age_text_is_normalized(visible_time, expected):
    html = f"""
    <li>
      <div class="base-card" data-entity-urn="urn:li:jobPosting:123">
        <h3 class="base-search-card__title">Software Engineer</h3>
        <h4 class="base-search-card__subtitle"><a>TechCorp</a></h4>
        <span class="job-search-card__location">Remote</span>
        <time datetime="2026-08-14">{visible_time}</time>
      </div>
    </li>
    """

    jobs, raw_count = _parse_linkedin_cards(html)

    assert raw_count == 1
    assert jobs[0]["_relative_age_text"] == expected
    assert jobs[0]["id"] == "123"
    assert jobs[0]["company"] == "TechCorp"
    assert jobs[0]["title"] == "Software Engineer"
    assert jobs[0]["location"] == "Remote"
    assert jobs[0]["date_posted"] == "2026-08-14"


@pytest.mark.parametrize(
    ("text", "expected_seconds"),
    [
        ("just now", 0),
        ("moments ago", 0),
        ("5 minutes ago", 300),
        ("6 hours ago", 21600),
        ("7 hours ago", 25200),
        ("1 day ago", 86400),
        ("2 weeks ago", 1209600),
        ("Reposted 3 hours ago", 10800),
        ("Posted 2 hours ago", 7200),
        ("", None),
        ("unknown text", None),
    ],
)
def test_linkedin_relative_age_seconds(text, expected_seconds):
    assert _linkedin_relative_age_seconds(text) == expected_seconds


def _synthetic_card(job_id: int, relative_age_text: str | None) -> str:
    time_html = "" if relative_age_text is None else (
        f'<time datetime="2026-09-18">{relative_age_text}</time>'
    )
    return f"""
    <li>
      <div class="base-card" data-entity-urn="urn:li:jobPosting:{job_id}">
        <h3 class="base-search-card__title">Software Engineer {job_id}</h3>
        <h4 class="base-search-card__subtitle"><a>TechCorp</a></h4>
        <span class="job-search-card__location">Remote</span>
        {time_html}
      </div>
    </li>
    """


@pytest.fixture
def linkedin_age_filter_html():
    return "".join([
        _synthetic_card(101, "5 hours ago"),
        _synthetic_card(102, "6 hours ago"),
        _synthetic_card(103, "7 hours ago"),
        _synthetic_card(104, "1 day ago"),
        _synthetic_card(105, None),
        _synthetic_card(106, "Recently posted"),
    ])


def _run_mocked_linkedin_search(monkeypatch, html, max_age_seconds):
    requested_urls = []

    def fake_fetch(url):
        requested_urls.append(url)
        return html

    monkeypatch.setattr(scrape_jobs, "fetch", fake_fetch)
    monkeypatch.setattr(scrape_jobs.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(scrape_jobs, "_RATE_LIMITED", False)
    jobs, raw_count = _linkedin_search(
        ["software"],
        21600,
        geos=[{"name": "Test", "location": "Test", "geoId": ""}],
        max_results=10,
        max_age_seconds=max_age_seconds,
    )
    assert len(requested_urls) == 1
    assert "f_TPR=r21600" in requested_urls[0]
    return jobs, raw_count


def test_linkedin_search_applies_conservative_local_age_filter(
        monkeypatch, capsys, linkedin_age_filter_html):
    jobs, raw_count = _run_mocked_linkedin_search(
        monkeypatch, linkedin_age_filter_html, max_age_seconds=21600
    )

    returned_ids = {job["url"].rstrip("/").rsplit("/", 1)[-1] for job in jobs}
    assert returned_ids == {"101", "102", "105", "106"}
    assert raw_count == 6
    assert all("_relative_age_text" not in job for job in jobs)
    assert (
        "LinkedIn local age filter: 4 kept, 2 proven older than 6h dropped, "
        "2 unknown-age kept"
    ) in capsys.readouterr().out


def test_linkedin_search_default_does_not_filter_relative_age(
        monkeypatch, linkedin_age_filter_html):
    jobs, raw_count = _run_mocked_linkedin_search(
        monkeypatch, linkedin_age_filter_html, max_age_seconds=None
    )

    returned_ids = {job["url"].rstrip("/").rsplit("/", 1)[-1] for job in jobs}
    assert returned_ids == {"101", "102", "103", "104", "105", "106"}
    assert raw_count == 6
