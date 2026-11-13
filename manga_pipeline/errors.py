"""Errors raised below the workflow layer and handled by it (SPEC "Parts", "Services")."""


class Unreachable(Exception):
    """A service the work needs cannot be reached. From a stage: the chapter stays ready at no attempt, and the part
    claims no chapter at that stage for the rest of the run; raised outside a stage, it ends the run."""

    def __init__(self, service, reason=""):
        super().__init__(f"{service}: {reason}" if reason else service)
        self.service = service
        self.reason = reason
