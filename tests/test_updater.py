"""app/updater.py — the GitHub release check.

Every test here runs without a network: urlopen is replaced with a fake that
returns a canned release payload, and the result cache is redirected into a
temp directory by conftest's app_dir fixture.
"""

import json
import time
import urllib.error

import pytest

from app import updater
from app.updater import ReleaseInfo, _is_newer_version, fetch_release_notes


class FakeResponse:
    """Minimal stand-in for the object urlopen returns in a `with` block."""

    def __init__(self, payload):
        self._body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._body


class FakeGitHub:
    """Serves canned release payloads and records whether it was asked."""

    def __init__(self, monkeypatch):
        self._monkeypatch = monkeypatch
        self.calls = []

    def serve(self, payload):
        def fake_urlopen(request, timeout=None):
            self.calls.append((request.full_url, timeout))
            if isinstance(payload, Exception):
                raise payload
            return FakeResponse(payload)
        self._monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)

    @property
    def called(self):
        return bool(self.calls)


@pytest.fixture
def github(monkeypatch):
    # Pinned so the comparison against the fake release tag doesn't move when
    # the app's own version is bumped.
    monkeypatch.setattr(updater, "__version__", "1.0.0")
    return FakeGitHub(monkeypatch)


def release(tag="v1.1.0", body="Fixed things"):
    return {"tag_name": tag, "body": body}


# ---------------------------------------------------------------------------
# version comparison
# ---------------------------------------------------------------------------

class TestIsNewerVersion:
    @pytest.mark.parametrize("current, latest", [
        ("1.0.0", "1.0.1"),
        ("1.0.0", "1.1.0"),
        ("1.0.0", "2.0.0"),
        ("1.0.0", "v1.0.1"),
        ("1.0.9", "1.1.0"),
        # Different lengths compare component by component, zero-padded.
        ("1.0", "1.0.1"),
        ("1.0.0", "1.1"),
        ("0.9.9", "0.10.0"),
    ])
    def test_newer(self, current, latest):
        assert _is_newer_version(current, latest) is True

    @pytest.mark.parametrize("current, latest", [
        ("1.0.0", "1.0.0"),
        ("1.0.0", "v1.0.0"),
        ("1.1.0", "1.0.0"),
        ("2.0.0", "1.9.9"),
        ("1.0.0", "1.0"),
        ("1.0", "1.0.0"),
    ])
    def test_not_newer(self, current, latest):
        assert _is_newer_version(current, latest) is False

    @pytest.mark.parametrize("current, latest", [
        ("", "1.0.0"),
        ("1.0.0", ""),
        ("dev", "1.0.0"),
        ("1.0.0", "nightly"),
        ("1.0.0-beta", "1.0.0"),
        ("a.b.c", "1.0.0"),
    ])
    def test_unparseable_versions_are_never_newer(self, current, latest):
        # A prerelease or dev version must not nag every user who runs it.
        assert _is_newer_version(current, latest) is False


class TestEndpoints:
    def test_api_url_targets_this_project(self):
        assert updater.API_URL == \
            f"https://api.github.com/repos/{updater.GITHUB_OWNER}/{updater.GITHUB_REPO}/releases/latest"

    def test_browser_url_is_the_releases_page(self):
        assert updater.BROWSER_URL == \
            f"https://github.com/{updater.GITHUB_OWNER}/{updater.GITHUB_REPO}/releases/latest"


# ---------------------------------------------------------------------------
# result cache
# ---------------------------------------------------------------------------

class TestCache:
    def test_missing_file_is_a_miss(self, app_dir):
        assert updater._load_cache() is None

    def test_fresh_entry_is_a_hit(self, app_dir):
        updater._save_cache("1.2.0")
        cache = updater._load_cache()
        assert cache["latest_version"] == "1.2.0"
        assert cache["checked_at"] <= time.time()

    def test_entry_older_than_a_day_is_a_miss(self, app_dir):
        updater._save_cache("1.2.0")
        app_dir.mkdir(parents=True, exist_ok=True)
        updater._CACHE_FILE.write_text(json.dumps({
            "checked_at": time.time() - updater._CACHE_TTL - 60,
            "latest_version": "1.2.0",
        }), encoding="utf-8")
        assert updater._load_cache() is None

    def test_corrupt_cache_is_a_miss(self, app_dir):
        app_dir.mkdir(parents=True, exist_ok=True)
        updater._CACHE_FILE.write_text("{ not json", encoding="utf-8")
        assert updater._load_cache() is None

    def test_unreadable_cache_is_a_miss(self, app_dir):
        app_dir.mkdir(parents=True, exist_ok=True)
        updater._CACHE_FILE.mkdir()
        assert updater._load_cache() is None

    def test_writing_the_cache_never_raises(self, app_dir, monkeypatch, tmp_path):
        # Never worth blocking the app over a cache write: an unwritable app
        # directory has to fail silently.
        blocker = tmp_path / "a-file-not-a-directory"
        blocker.write_text("occupied", encoding="utf-8")
        monkeypatch.setattr(updater, "APP_DIR", blocker / "nested")
        monkeypatch.setattr(updater, "_CACHE_FILE", blocker / "nested" / "update_cache.json")
        updater._save_cache("1.2.0")  # must not raise


def test_cache_path_sits_beside_the_rest_of_the_app_data(app_dir):
    assert updater._CACHE_FILE.parent == app_dir
    assert updater._CACHE_FILE.name == "update_cache.json"


# ---------------------------------------------------------------------------
# the check itself
# ---------------------------------------------------------------------------

class TestCheckForUpdates:
    def test_reports_a_newer_release(self, github, app_dir):
        github.serve(release("v1.1.0"))
        info = updater.check_for_updates()
        assert isinstance(info, ReleaseInfo)
        assert info.version == "1.1.0"
        assert info.url == updater.BROWSER_URL
        assert info.is_prerelease is False

    def test_asks_gitHub_not_somewhere_else(self, github, app_dir):
        github.serve(release("v1.1.0"))
        updater.check_for_updates()
        assert github.calls[0][0] == updater.API_URL

    def test_uses_a_bounded_timeout(self, github, app_dir):
        github.serve(release("v1.1.0"))
        updater.check_for_updates()
        assert github.calls[0][1] == 10

    def test_up_to_date_reports_nothing(self, github, app_dir):
        github.serve(release("v1.0.0"))
        assert updater.check_for_updates() is None

    def test_a_newer_local_build_reports_nothing(self, github, app_dir):
        github.serve(release("v0.9.0"))
        assert updater.check_for_updates() is None

    def test_a_release_with_no_tag_is_ignored(self, github, app_dir):
        github.serve({"body": "no tag here"})
        assert updater.check_for_updates() is None

    @pytest.mark.parametrize("failure", [
        urllib.error.URLError("offline"),
        urllib.error.HTTPError("url", 403, "rate limited", None, None),
        TimeoutError("timed out"),
        OSError("connection reset"),
    ])
    def test_network_failures_are_silent(self, github, app_dir, failure):
        # An offline user, or an API hiccup, must never turn into an error
        # dialog or a failed startup.
        github.serve(failure)
        assert updater.check_for_updates() is None

    def test_unreadable_response_is_silent(self, github, app_dir):
        github.serve(b"<html>not json</html>")
        assert updater.check_for_updates() is None

    def test_a_successful_check_is_cached(self, github, app_dir):
        github.serve(release("v1.0.0"))
        updater.check_for_updates()
        assert updater._CACHE_FILE.is_file()
        assert json.loads(updater._CACHE_FILE.read_text(encoding="utf-8"))["latest_version"] == "1.0.0"

    def test_a_failed_check_is_not_cached(self, github, app_dir):
        github.serve(urllib.error.URLError("offline"))
        updater.check_for_updates()
        assert not updater._CACHE_FILE.exists()

    def test_a_fresh_cache_avoids_a_second_request(self, github, app_dir):
        updater._save_cache("1.4.0")
        github.serve(release("v1.0.0"))  # would say "up to date" if it were asked
        info = updater.check_for_updates()
        assert info.version == "1.4.0"
        assert github.called is False

    def test_a_fresh_cache_saying_up_to_date_asks_nothing(self, github, app_dir):
        updater._save_cache("1.0.0")
        github.serve(release("v9.9.9"))
        assert updater.check_for_updates() is None
        assert github.called is False

    def test_a_stale_cache_forces_a_fresh_request(self, github, app_dir):
        app_dir.mkdir(parents=True, exist_ok=True)
        updater._CACHE_FILE.write_text(json.dumps({
            "checked_at": time.time() - updater._CACHE_TTL - 60,
            "latest_version": "1.0.0",
        }), encoding="utf-8")
        github.serve(release("v2.0.0"))
        assert updater.check_for_updates().version == "2.0.0"
        assert github.called is True

    def test_force_ignores_a_fresh_cache(self, github, app_dir):
        updater._save_cache("1.0.0")  # would report "up to date"
        github.serve(release("v2.0.0"))
        assert updater.check_for_updates(force=True).version == "2.0.0"
        assert github.called is True

    def test_release_notes_are_not_inlined_in_the_check_result(self, github, app_dir):
        # Fetching them is a separate, deliberate request.
        github.serve(release("v1.1.0", body="Detailed notes"))
        assert updater.check_for_updates().notes == ""


class TestFetchReleaseNotes:
    def test_returns_the_notes_for_the_matching_version(self, github, app_dir):
        github.serve(release("v1.1.0", body="Fixed the thing\nAnd another"))
        assert fetch_release_notes("1.1.0") == "Fixed the thing\nAnd another"

    def test_v_prefix_is_ignored_on_both_sides(self, github, app_dir):
        github.serve(release("v1.1.0", body="notes"))
        assert fetch_release_notes("1.1.0") == "notes"

    def test_returns_nothing_for_a_different_version(self, github, app_dir):
        github.serve(release("v1.1.0", body="notes"))
        assert fetch_release_notes("1.0.0") == ""

    def test_returns_nothing_when_the_request_fails(self, github, app_dir):
        github.serve(urllib.error.URLError("offline"))
        assert fetch_release_notes("1.1.0") == ""

    def test_returns_nothing_for_an_unreadable_response(self, github, app_dir):
        github.serve(b"not json")
        assert fetch_release_notes("1.1.0") == ""

    def test_a_release_without_a_body_reads_as_empty(self, github, app_dir):
        github.serve({"tag_name": "v1.1.0"})
        assert fetch_release_notes("1.1.0") == ""