"""Stand-in for the Discord webhook client: records each posted text, or raises what a test sets."""

from manga_pipeline.clients import discord

from tests.fakes.http import NoNetwork


class FakeDiscord(discord.Discord):
    """A Discord client that sends nothing.

    Each post is recorded in `posts`; while `failure` is set, a post raises it instead (and records nothing).
    """

    def __init__(self):
        super().__init__("https://discord.invalid/api/webhooks/0/fake", NoNetwork("discord"))
        self.posts: list[str] = []
        self.failure: BaseException | None = None

    def post(self, text):
        if self.failure is not None:
            raise self.failure
        self.posts.append(text)
