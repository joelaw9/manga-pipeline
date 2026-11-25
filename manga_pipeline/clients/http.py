"""The HTTP layer of every service call (SPEC "Services").

Retries, the per-service minimum gap, what counts as unreachable, and logging without secrets.

A client owns 1 Service per run. The Service remembers that it was found unreachable, so the run asks it no more.
"""

import email.message
import http.client
import json as jsonlib
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus

from manga_pipeline.errors import Unreachable

log = logging.getLogger(__name__)
WAITS = (2, 4, 8, 16)  # seconds before each retry of a failed request; their count is the retries
MAX_RETRY_AFTER = 60  # seconds; a service asking for longer is unreachable at once
UNREACHABLE_STATUS = frozenset({429, 502, 503, 504})
SECRET_WORDS = ("key", "token", "secret", "pass", "auth", "sig")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BODY_SHOWN = 200  # characters of an error reply shown in its message


class HttpError(Exception):
    """The service answered with a status that is a failure of the request (SPEC "Services")."""

    def __init__(self, status: int, url: str, body: bytes) -> None:
        """Store the reply's status and body; the message names the status, the redacted URL and the body's start."""
        text = body.decode("utf-8", "replace").strip()[:BODY_SHOWN]
        super().__init__(f"HTTP {status} from {redact_url(url)}" + (f": {text}" if text else ""))
        self.status = status
        self.body = body


class HttpTimeoutError(Exception):
    """A request sent with retry_timeout=False timed out; the service is not marked unreachable."""


class NotJsonError(HttpError):
    """A reply read as JSON is not JSON (SPEC "Services"): a bot check's HTML page, say.

    A failure of the request, as an HttpError is; its status is the reply's own.
    """

    def __init__(self, service: str, status: int, content_type: str | None, body: bytes) -> None:
        """Store the reply's status and body; the message names the service, the status and the content type."""
        Exception.__init__(self, f"{service}: HTTP {status} reply is not JSON (Content-Type {content_type})")
        self.status = status
        self.body = body


@dataclass(frozen=True)
class Response:
    """A service's reply to 1 request: the service's name, the status, the headers and the body."""

    service: str  # the name of the Service that got it
    status: int
    headers: email.message.Message  # the reply's headers: get(name) ignores case
    body: bytes

    def parse[T](self, read: Callable[[bytes], T]) -> T:
        """Return the body as read by `read`, the reader of the reply's JSON shape (it loads the body).

        NotJsonError naming the service, status and content type when the body is not JSON.
        """
        try:
            return read(self.body)
        except ValueError:
            raise NotJsonError(self.service, self.status, self.headers.get("Content-Type"), self.body) from None


def redact_url(url: str) -> str:
    """Return the URL without credentials.

    User info is dropped, and the value of every query parameter whose name contains a SECRET_WORDS word (in any case)
    replaced.
    """
    parts = urllib.parse.urlsplit(url)
    query = [
        (name, "REDACTED" if any(word in name.lower() for word in SECRET_WORDS) else value)
        for name, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    ]
    netloc = parts.netloc.rpartition("@")[2]
    return urllib.parse.urlunsplit(parts._replace(netloc=netloc, query=urllib.parse.urlencode(query)))


class Service:
    """1 service as 1 run sees it.

    Its name (what Unreachable carries), its minimum gap between requests in seconds, the User-Agent it sends, how its
    messages and logs show a URL (redact_url, or a function hiding a URL that is a secret whole), when it was last
    asked, and whether it was found unreachable.
    """

    def __init__(
        self, name: str, min_gap: float = 0.0, user_agent: str = USER_AGENT, shown: Callable[[str], str] = redact_url
    ) -> None:
        """Store the service's name, minimum gap, User-Agent and URL display; it starts never asked, not unreachable."""
        self.name = name
        self.min_gap = min_gap
        self.user_agent = user_agent
        self.shown = shown
        self._last: float | None = None
        self._down: str | None = None

    def mark_unreachable(self, reason: str) -> Unreachable:
        """Remember the service as unreachable for the rest of the run; returns the Unreachable to raise."""
        self._down = reason
        return Unreachable(self.name, reason)

    def request(  # noqa: PLR0913  # a request's own options, each passed by some caller
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: Mapping[str, object] | None = None,
        timeout: float = 60,
        retry_timeout: bool = True,
        waits: tuple[float, ...] = WAITS,
        min_gap: float | None = None,
    ) -> Response:
        """Send 1 request, retried as SPEC "Services" says, and return its Response.

        Raises Unreachable (and marks the service), HttpError, or HttpTimeoutError for a timeout when retry_timeout is
        false. waits are the seconds before each retry; waits=() sends it once: a 5xx reply other than
        UNREACHABLE_STATUS is then an HttpError. min_gap, when
        given, replaces the service's gap before this request (a call the service limits more). ValueError for a URL
        that is not http or https: a wrong setting, never retried.
        """
        if self._down is not None:
            raise Unreachable(self.name, self._down)
        shown = self.shown(url)
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            msg = f"{self.name}: not an http or https URL: {shown}"
            raise ValueError(msg)
        headers = {"User-Agent": self.user_agent, "Accept": "application/json", **(headers or {})}
        data = None
        if json is not None:
            data = jsonlib.dumps(json).encode()
            headers["Content-Type"] = "application/json"
        failed: tuple[int, bytes] | None = None  # bound here: the checker cannot see the loop runs
        retries = len(waits)
        for attempt in range(retries + 1):
            self._wait_for_gap(self.min_gap if min_gap is None else min_gap)
            wait = waits[attempt] if attempt < retries else 0
            failed = None  # (status, body) of an HTTP failure; None for a network one
            try:
                return _send(
                    self.name, urllib.request.Request(url, data=data, headers=headers, method=method), shown, timeout
                )
            except urllib.error.HTTPError as e:
                failed = (e.code, _error_body(e))
                asked = self._asked_wait(e, failed[1], shown)
                problem = f"HTTP {e.code}"
                if asked is not None:
                    wait = asked
            except (OSError, http.client.HTTPException) as e:
                problem = _network_problem(e, f"{method} {shown}", timeout, retry_timeout=retry_timeout)
            if attempt < retries:
                retry = f"retry {attempt + 1} of {retries} in {wait} s"
                log.info("%s: %s from %s; %s", self.name, problem, shown, retry)
                time.sleep(wait)
        if failed is not None and failed[0] not in UNREACHABLE_STATUS:
            raise HttpError(failed[0], shown, failed[1])
        msg = f"{problem} from {shown} after {retries} retries"
        raise self.mark_unreachable(msg)

    def _asked_wait(self, error: urllib.error.HTTPError, body: bytes, shown: str) -> int | None:
        """Return the seconds a retried HTTP failure's Retry-After asks for, None when it names none.

        HttpError for a status that is the request's failure (below 500, 429 aside); Unreachable (and the service
        marked) for a Retry-After above MAX_RETRY_AFTER.
        """
        if error.code != HTTPStatus.TOO_MANY_REQUESTS and error.code < HTTPStatus.INTERNAL_SERVER_ERROR:
            raise HttpError(error.code, shown, body) from None
        asked = _retry_after(error.headers)
        if asked is not None and asked > MAX_RETRY_AFTER:
            msg = f"HTTP {error.code} from {shown}, Retry-After {asked} s"
            raise self.mark_unreachable(msg) from None
        return asked

    def _wait_for_gap(self, min_gap: float) -> None:
        if self._last is not None:
            wait = self._last + min_gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        self._last = time.monotonic()


def _network_problem(
    error: OSError | http.client.HTTPException, sent: str, timeout: float, *, retry_timeout: bool
) -> str:
    """Return how the log names a network failure of the request sent ("METHOD shown URL").

    HttpTimeoutError for a timeout when retry_timeout is false.
    """
    timed_out = isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError)
    if timed_out and not retry_timeout:
        msg = f"{sent} timed out after {timeout} s"
        raise HttpTimeoutError(msg) from None
    return f"timed out after {timeout} s" if timed_out else f"{type(error).__name__}: {error}"


def _send(service: str, request: urllib.request.Request, shown: str, timeout: float) -> Response:
    """Send request (a urllib Request) once, logged at debug under its shown (redacted) URL."""
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
        log.debug("%s %s -> %s %d B %.3f s", request.get_method(), shown, outcome, size, time.monotonic() - start)


def _error_body(error: urllib.error.HTTPError) -> bytes:
    """Return the body of an error reply, for the message only.

    Empty when it cannot be read (the status is the answer).
    """
    try:
        return error.read()
    except (OSError, http.client.HTTPException):
        return b""


def _retry_after(headers: email.message.Message | None) -> int | None:
    """Return the seconds a Retry-After header names; None when it is absent or a date."""
    value = (headers.get("Retry-After") or "").strip() if headers is not None else ""
    return int(value) if value.isdigit() else None
