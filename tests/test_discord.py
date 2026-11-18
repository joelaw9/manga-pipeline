"""clients.discord: the posted body, the webhook URL never shown (SPEC "Services", "Needs human")."""

import json
import logging

import pytest

from manga_pipeline import config
from manga_pipeline.clients import discord, http
from manga_pipeline.errors import Unreachable

from tests.fakes.http import Clock, Reply, Urlopen, http_error
from tests.timing import budget

WEBHOOK = "https://discord.invalid/api/webhooks/123/secret-token"


@pytest.fixture
def urlopen(monkeypatch):
    def script(*items):
        opener = Urlopen(*items)
        monkeypatch.setattr(http.urllib.request, "urlopen", opener)
        return opener

    monkeypatch.setattr(http, "time", Clock())
    return script


@budget(load="pure")
def test_a_post_sends_plain_text_that_pings_no_one_and_embeds_no_link(urlopen):
    sent = urlopen(Reply(b"", status=204))
    discord.Discord(WEBHOOK).post("series 5 (Name): reason -> retry --series 5")
    request = sent.requests[0]
    assert request.get_method() == "POST"
    assert request.full_url == WEBHOOK
    body = json.loads(request.data)
    assert body == {
        "content": "series 5 (Name): reason -> retry --series 5",
        "allowed_mentions": {"parse": []},
        "flags": 4,
    }, "SPEC 'Needs human': plain text, no pings, no link previews"
    assert sent.timeouts == [discord.TIMEOUT], "a silent Discord holds export's lock briefly"


@budget(load="pure")
def test_a_refused_post_never_shows_the_webhook(urlopen, caplog):
    urlopen(http_error(503), http_error(404, b'{"message": "Unknown Webhook"}'))
    with caplog.at_level(logging.DEBUG, logger="manga_pipeline"), pytest.raises(http.HttpError) as refused:
        discord.Discord(WEBHOOK).post("text")
    assert "secret-token" not in str(refused.value), "SPEC 'Services': the webhook URL is a secret whole"
    assert "123" not in str(refused.value)
    assert "discord webhook" in str(refused.value)
    assert "secret-token" not in caplog.text, "no log line carries the webhook"
    assert "retry 1 of 1" in caplog.text, "1 retry"


@budget(load="pure")
def test_an_unreachable_discord_never_shows_the_webhook(urlopen, caplog):
    urlopen(http_error(503), http_error(503))
    with caplog.at_level(logging.DEBUG, logger="manga_pipeline"), pytest.raises(Unreachable) as down:
        discord.Discord(WEBHOOK).post("text")
    assert "secret-token" not in str(down.value)
    assert "secret-token" not in caplog.text


@budget(load="pure")
def test_no_webhook_means_no_client():
    cfg = config.cached()
    assert discord.from_config({**cfg, "notify": {"discord_webhook": None}}) is None, "Discord off"
    assert isinstance(discord.from_config({**cfg, "notify": {"discord_webhook": WEBHOOK}}), discord.Discord)
