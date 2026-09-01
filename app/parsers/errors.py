class SourcePageError(ValueError):
    """Rendered source page is not safe to ingest."""


class ChallengePageError(SourcePageError):
    """Rendered source page is an access challenge."""


def reject_challenge(html: str) -> None:
    sample = html[:20_000].lower()
    markers = ("just a moment", "verify you are human", "cf-chl-")
    if any(marker in sample for marker in markers):
        raise ChallengePageError("source challenge detected")
