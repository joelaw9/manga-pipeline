"""Stand-ins for the HTTP transport: a clock, urlopen's replies, and a Service that answers from canned replies."""

import email.message
import io
import json
import urllib.error
import urllib.parse

from manga_pipeline.clients import http
from manga_pipeline.errors import Unreachable


class Clock:
    """Stands in for the time module in clients.http: sleep advances monotonic."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def headers(values=None):
    message = email.message.Message()
    for name, value in (values or {}).items():
        message[name] = value
    return message


class Reply:
    """What urllib.request.urlopen returns: a context manager with status, headers and read()."""

    def __init__(self, body: object = b"{}", status=200, header_values=None):
        self.status = status
        self.headers = headers({"Content-Type": "application/json", **(header_values or {})})
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def http_error(status, body=b"", header_values=None):
    return urllib.error.HTTPError("http://service.invalid", status, "error", headers(header_values), io.BytesIO(body))


class Urlopen:
    """Stands in for urllib.request.urlopen: answers each call with the next scripted item (a Reply to return or an
    exception to raise) and records the requests.
    """

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []
        self.timeouts = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def reply(value, header_values=None, service="canned"):
    """An http.Response of service carrying value: bytes as they are, anything else as JSON."""
    body = value if isinstance(value, bytes) else json.dumps(value).encode()
    content_type = "application/octet-stream" if isinstance(value, bytes) else "application/json"
    return http.Response(service, 200, headers({"Content-Type": content_type, **(header_values or {})}), body)


class Each(list):
    """Answers of a CannedService entry, 1 per request in order."""


class CannedService(http.Service):
    """A Service that sends nothing: each request is recorded in `sent` as (method, path with query, json) and
    answered by the first entry of `answers` ({path prefix: value}) its path starts with. A value is the reply's JSON,
    bytes for a raw body, an http.Response, an exception to raise, or a callable(method, path, json) returning one of
    those; an Each is used up 1 item per request. No entry = AssertionError.
    """

    def __init__(self, name, answers=None):
        super().__init__(name)
        self.answers = answers or {}
        self.sent = []
        self.options = []  # (timeout, retry_timeout) per request

    def request(  # noqa: PLR0913  # mirrors the real Service.request signature
        self, method, url, *, headers=None, json=None, timeout=60, retry_timeout=True, retries=None
    ):
        del retries  # the real request's parameter, passed by keyword; the fake does not retry
        if self._down is not None:
            raise Unreachable(self.name, self._down)
        parts = urllib.parse.urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        self.sent.append((method, path, json))
        self.options.append((timeout, retry_timeout))
        self.headers = headers
        for prefix, answer in self.answers.items():
            if path.startswith(prefix):
                value = answer.pop(0) if isinstance(answer, Each) else answer
                if callable(value):
                    value = value(method, path, json)
                if isinstance(value, BaseException):
                    raise value
                return value if isinstance(value, http.Response) else reply(value, service=self.name)
        msg = f"CannedService {self.name}: no answer for {method} {path}"
        raise AssertionError(msg)


class NoNetwork(http.Service):
    """The Service of a fake client: any request means the fake misses an override."""

    def request(self, method, url, **_kwargs):
        msg = f"{self.name} fake: unexpected request {method} {http.redact_url(url)}"
        raise AssertionError(msg)
