"""Discord webhook client (SPEC "Services", "Needs human").

The webhook URL is a secret whole (its token is a path segment): its Service shows it as SHOWN_URL in every message and
log line.
"""

from manga_pipeline import config
from manga_pipeline.clients import http

SHOWN_URL = "discord webhook"
TIMEOUT = 10  # seconds; a send holds export's lock, so a silent Discord costs at most 2 of these
RETRIES = 1
MIN_GAP = 1.0  # seconds between posts: under Discord's per-webhook limit
USER_AGENT = "manga_pipeline"
NO_MENTIONS: dict[str, list[str]] = {"parse": []}  # a name holding @everyone pings no one
SUPPRESS_EMBEDS = 1 << 2  # a URL in a reason shows no link preview


class Discord:
    """A run's Discord webhook client: posts plain-text messages over 1 Service."""

    def __init__(self, url: str, service: http.Service | None = None) -> None:
        """Store the webhook URL and the Service its posts go through."""
        self.url = url
        self._service = service or http.Service(
            "discord", min_gap=MIN_GAP, user_agent=USER_AGENT, shown=lambda _url: SHOWN_URL
        )

    def post(self, text: str) -> None:
        """Post 1 message; any 2xx reply is success.

        Unreachable (not reached, 429, 5xx: the run asks no more) or HttpError (Discord refused the post).
        """
        self._service.request(
            "POST",
            self.url,
            json={"content": text, "allowed_mentions": NO_MENTIONS, "flags": SUPPRESS_EMBEDS},
            timeout=TIMEOUT,
            retries=RETRIES,
        )


def from_config(cfg: config.Config) -> Discord | None:
    """Build the Discord client from notify.discord_webhook; None when it is not set (Discord off)."""
    url = cfg["notify"]["discord_webhook"]
    return None if url is None else Discord(url)
