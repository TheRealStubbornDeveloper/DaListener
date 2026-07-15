from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit


class CaptureCategory(StrEnum):
    MEETING = "meeting"
    MEDIA = "media"
    OTHER = "other"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class SourceClassification:
    category: CaptureCategory
    domain: str
    service_label: str
    supported: bool = True


MEETING_DOMAINS = {
    "zoom.us": "Zoom",
    "meet.google.com": "Google Meet",
    "teams.microsoft.com": "Microsoft Teams",
    "teams.live.com": "Microsoft Teams",
    "webex.com": "Webex",
}

MEDIA_DOMAINS = {
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "vimeo.com": "Vimeo",
    "twitch.tv": "Twitch",
}


def _domain_matches(host: str, registered: str) -> bool:
    return host == registered or host.endswith(f".{registered}")


def classify_source(url: str) -> SourceClassification:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return SourceClassification(CaptureCategory.UNSUPPORTED, "", "Unsupported page", False)

    scheme = parsed.scheme.lower()
    if scheme == "file":
        return SourceClassification(CaptureCategory.OTHER, "local-files", "Local file")
    if scheme not in {"http", "https"}:
        return SourceClassification(CaptureCategory.UNSUPPORTED, "", "Browser page", False)

    try:
        host = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        host = ""
    if not host:
        return SourceClassification(CaptureCategory.UNSUPPORTED, "", "Unsupported page", False)

    for domain, label in MEETING_DOMAINS.items():
        if _domain_matches(host, domain):
            return SourceClassification(CaptureCategory.MEETING, domain, label)
    for domain, label in MEDIA_DOMAINS.items():
        if _domain_matches(host, domain):
            return SourceClassification(CaptureCategory.MEDIA, domain, label)
    return SourceClassification(CaptureCategory.OTHER, host, host)


def warning_message(source: SourceClassification) -> str:
    return (
        f"{source.service_label} is not recognized as a live meeting. "
        "DaListener will send this tab's audio to OpenAI and API charges may apply. "
        "Make sure you have permission to transcribe this content."
    )


def classify_shared_label(label: str) -> SourceClassification:
    """Best-effort classification after the browser's native picker.

    getDisplayMedia deliberately does not reveal the selected tab URL. The
    browser-provided track label is available only after the user grants
    access, so known services are recognized from that label and everything
    else remains a supported generic source.
    """
    normalized = label.casefold()
    meetings = (
        ("zoom", "Zoom"), ("google meet", "Google Meet"),
        ("microsoft teams", "Microsoft Teams"), ("teams", "Microsoft Teams"),
        ("webex", "Webex"),
    )
    media = (("youtube", "YouTube"), ("vimeo", "Vimeo"), ("twitch", "Twitch"))
    for needle, service in meetings:
        if needle in normalized:
            return SourceClassification(CaptureCategory.MEETING, "browser-share", service)
    for needle, service in media:
        if needle in normalized:
            return SourceClassification(CaptureCategory.MEDIA, "browser-share", service)
    return SourceClassification(CaptureCategory.OTHER, "browser-share", "Shared browser tab")
