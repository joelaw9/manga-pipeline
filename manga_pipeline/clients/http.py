"""The HTTP layer of every service call (SPEC "Services"): retries, the per-service minimum gap, what counts as
unreachable, and logging without secrets.

A client owns 1 Service per run. The Service remembers that it was found unreachable, so the run asks it no more.
"""

import http.client
import json as jsonlib
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from manga_pipeline.errors import Unreachable

log = logging.getLogger(__name__)
RETRIES = 4
MAX_RETRY_AFTER = 60  # seconds; a service asking for longer is unreachable at once
UNREACHABLE_STATUS = frozenset({429, 502, 503, 504})
SECRET_WORDS = ("key", "token", "secret", "pass", "auth", "sig")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BODY_SHOWN = 200  # characters of an error reply shown in its message


class HttpError(Exception):
    """The service answered with a status that is a failure of the request (SPEC "Services")."""

    def __init__(self, status, url, body):
        text = body.decode("utf-8", "replace").strip()[:BODY_SHOWN]
        super().__init__(f"HTTP {status} from {redact_url(url)}" + (f": {text}" if text else ""))
        self.status = status
        self.body = body


class Timeout(Exception):
    """A request sent with retry_timeout=False timed out; the service is not marked unreachable."""


class NotJson(HttpError):
    """A reply read as JSON is not JSON (SPEC "Services"): a bot check's HTML page, say. A failure of the request, as
    an HttpError is; its status is the reply's own."""

    def __init__(self, service, status, content_type, body):
        Exception.__init__(self, f"{service}: HTTP {status} reply is not JSON (Content-Type {content_type})")
        self.status = status
        self.body = body


@dataclass(frozen=True)
class Response:
    service: str  # the name of the Service that got it
    status: int
    headers: Any  # the reply's headers: get(name) ignores case
    body: bytes

    def json(self):
        """The body as JSON; NotJson naming the service, status and content type when it is not."""
        try:
            return jsonlib.loads(self.body)
        except ValueError:
            raise NotJson(self.service, self.status, self.headers.get("Content-Type"), self.body) from None


def redact_url(url):
    """The URL without credentials: user info dropped, and the value of every query parameter whose name contains a
    SECRET_WORDS word (in any case) replaced."""
    parts = urllib.parse.urlsplit(url)
    query = [
        (name, "REDACTED" if any(word in name.lower() for word in SECRET_WORDS) else value)
        for name, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    ]
    netloc = parts.netloc.rpartition("@")[2]
    return urllib.parse.urlunsplit(parts._replace(netloc=netloc, query=urllib.parse.urlencode(query)))


class Service:
    """1 service as 1 run sees it: its name (what Unreachable carries), its minimum gap between requests in seconds,
    the User-Agent it sends, when it was last asked, and whether it was found unreachable."""

    def __init__(self, name, min_gap=0.0, user_agent=USER_AGENT):
        self.name = name
        self.min_gap = min_gap
        self.user_agent = user_agent
        self._last = None
        self._down = None

    def mark_unreachable(self, reason):
        """Remember the service as unreachable for the rest of the run; returns the Unreachable to raise."""
        self._down = reason
        return Unreachable(self.name, reason)

    def request(self, method, url, *, headers=None, json=None, timeout=60, retry_timeout=True, retries=RETRIES):
        """Send 1 request, retried as SPEC "Services" says, and return its Response. Raises Unreachable (and marks
        the service), HttpError, or Timeout for a timeout when retry_timeout is false. retries=0 sends it once: a 5xx
        reply other than UNREACHABLE_STATUS is then an HttpError. ValueError for a URL that is
        not http or https: a wrong setting, never retried."""
        if self._down is not None:
            raise Unreachable(self.name, self._down)
        shown = redact_url(url)
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            msg = f"{self.name}: not an http or https URL: {shown}"
            raise ValueError(msg)
        headers = {"User-Agent": self.user_agent, "Accept": "application/json", **(headers or {})}
        data = None
        if json is not None:
            data = jsonlib.dumps(json).encode()
            headers["Content-Type"] = "application/json"
        for attempt in range(retries + 1):
            self._wait_for_gap()
            wait = 2**attempt * 2
            failed = None  # (status, body) of an HTTP failure; None for a network one
            try:
                return _send(self.name, method, url, shown, headers, data, timeout)
            except urllib.error.HTTPError as e:
                failed = (e.code, _error_body(e))
                if e.code != 429 and e.code < 500:
                    raise HttpError(e.code, url, failed[1]) from None
                problem = f"HTTP {e.code}"
                asked = _retry_after(e.headers)
                if asked is not None:
                    if asked > MAX_RETRY_AFTER:
                        msg = f"{problem} from {shown}, Retry-After {asked} s"
                        raise self.mark_unreachable(msg) from None
                    wait = asked
            except (OSError, http.client.HTTPException) as e:
                timed_out = isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError)
                if timed_out and not retry_timeout:
                    msg = f"{method} {shown} timed out after {timeout} s"
                    raise Timeout(msg) from None
                problem = f"timed out after {timeout} s" if timed_out else f"{type(e).__name__}: {e}"
            if attempt < retries:
                retry = f"retry {attempt + 1} of {retries} in {wait} s"
                log.info("%s: %s from %s; %s", self.name, problem, shown, retry)
                time.sleep(wait)
        if failed is not None and failed[0] not in UNREACHABLE_STATUS:
            raise HttpError(failed[0], url, failed[1])
        msg = f"{problem} from {shown} after {retries} retries"
        raise self.mark_unreachable(msg)

    def _wait_for_gap(self):
        if self._last is not None:
            wait = self._last + self.min_gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        self._last = time.monotonic()


def _send(service, method, url, shown, headers, data, timeout):
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    start = time.monotonic()
    outcome, size = None, 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            body = reply.read()
            outcome, size = reply.status, len(body)
            return Response(service, reply.status, reply.headers, body)
    except urllib.error.HTTPError as e:
        outcome = e.code
        raise
    except Exception as e:
        outcome = type(e).__name__
        raise
    finally:
        log.debug("%s %s -> %s %d B %.3f s", method, shown, outcome, size, time.monotonic() - start)


def _error_body(error):
    """The body of an error reply, for the message only: empty when it cannot be read (the status is the answer)."""
    try:
        return error.read()
    except (OSError, http.client.HTTPException):
        return b""


def _retry_after(headers):
    """The seconds a Retry-After header names; None when it is absent or a date."""
    value = (headers.get("Retry-After") or "").strip() if headers is not None else ""
    return int(value) if value.isdigit() else None
