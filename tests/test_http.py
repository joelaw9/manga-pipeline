"""clients.http: retries, back-off, Retry-After, the minimum gap, what is unreachable, redaction (SPEC "Services")."""

import json
import logging
import urllib.error

import pytest

from manga_pipeline.clients import http
from manga_pipeline.errors import Unreachable
from manga_pipeline.metadata import sources

from tests.fakes.http import Clock, Reply, Urlopen, http_error
from tests.timing import budget

URL = "http://service.invalid/api/thing"


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(http, "time", clock)
    return clock


def scripted(monkeypatch, *script):
    urlopen = Urlopen(*script)
    monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
    return urlopen


@budget(load="pure")
def test_reply_is_returned_with_status_headers_and_body(monkeypatch, clock):
    urlopen = scripted(monkeypatch, Reply({"a": 1}))
    got = http.Service("svc").request("POST", URL, json={"q": 2}, headers={"X-Extra": "1"}, timeout=7)
    assert (got.status, got.parse(json.loads), got.headers.get("content-type")) == (200, {"a": 1}, "application/json")
    sent = urlopen.requests[0]
    assert (sent.get_method(), sent.full_url, sent.data) == ("POST", URL, b'{"q": 2}')
    assert sent.get_header("Content-type") == "application/json"
    assert sent.get_header("X-extra") == "1"
    assert urlopen.timeouts == [7], "the caller's timeout reaches the transport"
    assert clock.sleeps == [], "a first-time success never sleeps"


@budget(load="pure")
@pytest.mark.usefixtures("clock")
def test_a_reply_that_is_not_json_fails_naming_service_status_and_content_type(monkeypatch):
    page = b"<html><title>MangaDex - Browser verification</title></html>"
    scripted(monkeypatch, Reply(page, header_values={"Content-Type": "text/html"}))
    got = http.Service("svc").request("GET", URL)
    with pytest.raises(
        http.NotJsonError, match=r"^svc: HTTP 200 reply is not JSON \(Content-Type text/html\)$"
    ) as caught:
        got.parse(json.loads)
    assert isinstance(caught.value, http.HttpError), "a reply that is not JSON is a failure of the request"


@budget(load="pure")
@pytest.mark.usefixtures("clock")
def test_a_service_sends_its_own_user_agent_else_the_browser_one(monkeypatch):
    urlopen = scripted(monkeypatch, Reply(), Reply())
    http.Service("named", user_agent="manga-pipeline").request("GET", URL)
    http.Service("plain").request("GET", URL)
    sent = [r.get_header("User-agent") for r in urlopen.requests]
    assert sent == ["manga-pipeline", http.USER_AGENT], "a Service's user_agent replaces the default"


@budget(load="pure")
@pytest.mark.usefixtures("clock")
def test_mangadex_is_asked_with_a_user_agent_naming_the_client(monkeypatch):
    urlopen = scripted(monkeypatch, Reply({"data": {}}), Reply(b"image"))
    client = sources.MangaDex()
    client.manga("abc")
    client.cover_image("abc", "c.jpg")
    sent = [(r.host, r.get_header("User-agent")) for r in urlopen.requests]
    assert sent == [("api.mangadex.org", "manga-pipeline"), ("uploads.mangadex.org", "manga-pipeline")], (
        "MangaDex answers a browser User-Agent with a bot check, on both hosts"
    )


@budget(load="pure")
@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("timed out"),
        urllib.error.URLError(TimeoutError("timed out")),
        urllib.error.URLError(ConnectionRefusedError("refused")),
        ConnectionResetError("reset"),
        http_error(429),
        http_error(500),
        http_error(503),
    ],
    ids=["read-timeout", "connect-timeout", "refused", "reset", "429", "500", "503"],
)
def test_a_failed_request_is_sent_again_and_its_answer_returned(monkeypatch, clock, failure):
    urlopen = scripted(monkeypatch, failure, Reply({"ok": True}))
    assert http.Service("svc").request("GET", URL).parse(json.loads) == {"ok": True}, "SPEC 'Services': retried"
    assert len(urlopen.requests) == 2
    assert clock.sleeps == [2]


@budget(load="pure")
def test_back_off_doubles_over_4_retries_then_the_service_is_unreachable(monkeypatch, clock):
    urlopen = scripted(monkeypatch, *[TimeoutError("timed out")] * 5)
    service = http.Service("svc")
    with pytest.raises(Unreachable) as e:
        service.request("GET", URL + "?apiKey=hunter2")
    assert e.value.service == "svc"
    assert "timed out" in str(e.value)
    assert "hunter2" not in str(e.value)
    assert len(urlopen.requests) == 5, "SPEC 'Services': 4 retries, 2 4 8 16 s"
    assert clock.sleeps == [2, 4, 8, 16], "SPEC 'Services': 4 retries, 2 4 8 16 s"


@budget(load="pure")
@pytest.mark.parametrize("status", [429, 502, 503, 504])
@pytest.mark.usefixtures("clock")
def test_these_statuses_after_the_retries_are_unreachable(monkeypatch, status):
    scripted(monkeypatch, *[http_error(status) for _ in range(5)])
    with pytest.raises(Unreachable, match=f"HTTP {status}"):
        http.Service("svc").request("GET", URL)


@budget(load="pure")
@pytest.mark.usefixtures("clock")
def test_http_500_after_the_retries_is_a_failure_not_unreachable(monkeypatch):
    urlopen = scripted(monkeypatch, *[http_error(500, b"boom") for _ in range(5)])
    service = http.Service("svc")
    with pytest.raises(http.HttpError) as e:
        service.request("GET", URL)
    assert (e.value.status, e.value.body) == (500, b"boom")
    assert len(urlopen.requests) == 5
    scripted(monkeypatch, Reply())
    assert service.request("GET", URL).status == 200, "a failed request does not mark the service unreachable"


@budget(load="pure")
@pytest.mark.parametrize("status", [501, 505, 507])
@pytest.mark.usefixtures("clock")
def test_other_statuses_of_500_or_above_fail_after_their_retries(monkeypatch, status):
    urlopen = scripted(monkeypatch, *[http_error(status) for _ in range(5)])
    with pytest.raises(http.HttpError) as e:
        http.Service("svc").request("GET", URL)
    assert e.value.status == status, "SPEC 'Services': 500 or above is retried"
    assert len(urlopen.requests) == 5, "SPEC 'Services': 500 or above is retried"


@budget(load="pure")
def test_an_error_reply_whose_body_cannot_be_read_is_still_retried(monkeypatch, clock):
    broken = http_error(503)
    monkeypatch.setattr(broken, "read", lambda: (_ for _ in ()).throw(TimeoutError("timed out")))
    urlopen = scripted(monkeypatch, broken, Reply())
    assert http.Service("svc").request("GET", URL).status == 200
    assert len(urlopen.requests) == 2
    assert clock.sleeps == [2]


@budget(load="pure")
@pytest.mark.parametrize("url", ["localhost:5000/api", "file:///etc/passwd", "kavita.invalid/api?apiKey=hunter2"])
def test_a_url_that_is_not_http_is_refused_at_once(monkeypatch, clock, url):
    urlopen = scripted(monkeypatch)
    service = http.Service("svc")
    with pytest.raises(ValueError, match="not an http or https URL") as e:
        service.request("GET", url)
    assert urlopen.requests == [], "a wrong setting is not a service that is down"
    assert clock.sleeps == [], "a wrong setting is not a service that is down"
    assert "hunter2" not in str(e.value)
    scripted(monkeypatch, Reply())
    assert service.request("GET", URL).status == 200, "the service is not marked unreachable"


@budget(load="pure")
@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_other_4xx_fail_at_once(monkeypatch, clock, status):
    urlopen = scripted(monkeypatch, http_error(status, b"why"))
    with pytest.raises(http.HttpError) as e:
        http.Service("svc").request("GET", URL + "?token=abc")
    assert e.value.status == status
    assert len(urlopen.requests) == 1
    assert clock.sleeps == []
    assert "why" in str(e.value), "the message shows the reply, never a secret"
    assert "abc" not in str(e.value), "the message shows the reply, never a secret"


@budget(load="pure")
def test_retry_after_seconds_replace_the_back_off(monkeypatch, clock):
    scripted(monkeypatch, http_error(429, header_values={"Retry-After": "7"}), Reply())
    http.Service("svc").request("GET", URL)
    assert clock.sleeps == [7]


@budget(load="pure")
def test_retry_after_as_a_date_keeps_the_back_off(monkeypatch, clock):
    scripted(monkeypatch, http_error(503, header_values={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), Reply())
    http.Service("svc").request("GET", URL)
    assert clock.sleeps == [2]


@budget(load="pure")
def test_retry_after_above_the_cap_is_unreachable_at_once(monkeypatch, clock):
    urlopen = scripted(monkeypatch, http_error(429, header_values={"Retry-After": "61"}))
    with pytest.raises(Unreachable, match="Retry-After 61"):
        http.Service("svc").request("GET", URL)
    assert len(urlopen.requests) == 1, "SPEC 'Services': above 60 s, at once"
    assert clock.sleeps == [], "SPEC 'Services': above 60 s, at once"


@budget(load="pure")
def test_retry_after_at_the_cap_is_waited(monkeypatch, clock):
    scripted(monkeypatch, http_error(429, header_values={"Retry-After": "60"}), Reply())
    http.Service("svc").request("GET", URL)
    assert clock.sleeps == [60]


@budget(load="pure")
@pytest.mark.usefixtures("clock")
def test_an_unreachable_service_is_not_asked_again(monkeypatch):
    urlopen = scripted(monkeypatch, *[TimeoutError("timed out")] * 5)
    service = http.Service("svc")
    with pytest.raises(Unreachable):
        service.request("GET", URL)
    with pytest.raises(Unreachable, match="timed out"):
        service.request("GET", URL)
    assert len(urlopen.requests) == 5, "SPEC 'Services': the run does not ask it again"


@budget(load="pure")
@pytest.mark.usefixtures("clock")
def test_another_service_object_is_asked(monkeypatch):
    scripted(monkeypatch, *[TimeoutError("timed out")] * 5, Reply())
    with pytest.raises(Unreachable):
        http.Service("svc").request("GET", URL)
    assert http.Service("svc").request("GET", URL).status == 200, "the mark belongs to 1 run's Service object"


@budget(load="pure")
@pytest.mark.usefixtures("clock")
def test_a_client_can_mark_the_service_unreachable(monkeypatch):
    urlopen = scripted(monkeypatch)
    service = http.Service("svc")
    error = service.mark_unreachable("HTTP 401 twice")
    assert (error.service, error.reason) == ("svc", "HTTP 401 twice")
    with pytest.raises(Unreachable, match="HTTP 401 twice"):
        service.request("GET", URL)
    assert urlopen.requests == []


@budget(load="pure")
@pytest.mark.parametrize(
    "failure",
    [TimeoutError("timed out"), urllib.error.URLError(TimeoutError("timed out"))],
    ids=["read", "connect"],
)
def test_a_timeout_not_to_be_retried_raises_timeout_and_marks_nothing(monkeypatch, clock, failure):
    urlopen = scripted(monkeypatch, failure, Reply())
    service = http.Service("svc")
    with pytest.raises(http.HttpTimeoutError):
        service.request("GET", URL, retry_timeout=False)
    assert len(urlopen.requests) == 1
    assert clock.sleeps == []
    assert service.request("GET", URL).status == 200, "the service is still asked"


@budget(load="pure")
@pytest.mark.usefixtures("clock")
def test_other_failures_are_still_retried_without_timeout_retry(monkeypatch):
    urlopen = scripted(monkeypatch, ConnectionResetError("reset"), Reply())
    assert http.Service("svc").request("GET", URL, retry_timeout=False).status == 200
    assert len(urlopen.requests) == 2


@budget(load="pure")
def test_minimum_gap_is_waited_between_requests(monkeypatch, clock):
    scripted(monkeypatch, Reply(), Reply(), Reply())
    service = http.Service("svc", min_gap=2.0)
    service.request("GET", URL)
    assert clock.sleeps == [], "the first request does not wait"
    clock.now += 0.5
    service.request("GET", URL)
    assert clock.sleeps == [1.5], "the rest of the gap is waited"
    clock.now += 5
    service.request("GET", URL)
    assert clock.sleeps == [1.5], "a gap already passed is not waited"


@budget(load="pure")
def test_no_gap_never_sleeps(monkeypatch, clock):
    scripted(monkeypatch, Reply(), Reply())
    service = http.Service("svc")
    service.request("GET", URL)
    service.request("GET", URL)
    assert clock.sleeps == []


@budget(load="pure")
@pytest.mark.parametrize(
    ("url", "want"),
    [
        ("http://h/a?apiKey=s3cret&pluginName=x", "http://h/a?apiKey=REDACTED&pluginName=x"),
        ("http://h/a?APIKEY=s3cret", "http://h/a?APIKEY=REDACTED"),
        ("http://h/a?access_token=t&page=2", "http://h/a?access_token=REDACTED&page=2"),
        ("http://h/a?Password=p&sig=q&auth=r", "http://h/a?Password=REDACTED&sig=REDACTED&auth=REDACTED"),
        ("http://user:pw@h:8080/a?x=1", "http://h:8080/a?x=1"),
        ("http://h/a", "http://h/a"),
        ("http://h/a?seriesId=5&empty=", "http://h/a?seriesId=5&empty="),
    ],
)
def test_redact_url(url, want):
    assert http.redact_url(url) == want


@budget(load="pure")
@pytest.mark.usefixtures("clock")
def test_requests_are_logged_at_debug_without_secrets(monkeypatch, caplog):
    scripted(monkeypatch, http_error(503), Reply(b"12345"))
    with caplog.at_level(logging.DEBUG, logger="manga_pipeline"):
        http.Service("svc").request("GET", URL + "?apiKey=hunter2")
    assert "hunter2" not in caplog.text, "SPEC 'Services': no log line carries a secret"
    assert "GET http://service.invalid/api/thing?apiKey=REDACTED -> 503" in caplog.text
    assert "-> 200 5 B" in caplog.text
    assert "retry 1 of 4 in 2 s" in caplog.text


@budget(load="pure")
def test_a_mangabaka_search_waits_its_longer_gap_after_any_mangabaka_request(monkeypatch, clock):
    scripted(monkeypatch, Reply({"data": {"id": 1}}), Reply({"data": []}))
    client = sources.MangaBaka()
    client.series("1")
    client.search("Solo Leveling")
    assert clock.sleeps == [pytest.approx(2.1)], (
        "MangaBaka is 1 service: its search keeps 2.1 s from the record call before it"
    )


@budget(load="pure")
@pytest.mark.usefixtures("clock")
def test_mangabaka_found_unreachable_by_a_search_is_unreachable_for_its_records(monkeypatch):
    urlopen = scripted(monkeypatch, *[http_error(503) for _ in range(http.RETRIES + 1)])
    client = sources.MangaBaka()
    with pytest.raises(Unreachable):
        client.search("Solo Leveling")
    with pytest.raises(Unreachable):
        client.series("1")
    assert len(urlopen.requests) == http.RETRIES + 1, "MangaBaka found unreachable is asked nothing more in the run"
