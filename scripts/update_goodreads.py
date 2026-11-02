"""Refresh Goodreads sections without losing the last successful snapshot."""

import argparse
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

SHELVES = {
    "currently-reading": "GOODREADS-LIST",
    "read": "GOODREADS-READ-LIST",
    "to-read": "GOODREADS-TO-READ-LIST",
}
MAX_ATTEMPTS = 3
TIMEOUT_SECONDS = 15
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_FEED_BYTES = 5 * 1024 * 1024


class FeedError(Exception):
    """Invalid configuration or an unexpected source response."""


class FeedUnavailable(FeedError):
    """An unavailable external feed; preserve the existing snapshot."""


def fetch_shelf(user_id, shelf):
    query = urlencode({"shelf": shelf, "per_page": 200})
    url = f"https://www.goodreads.com/review/list_rss/{user_id}?{query}"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(MAX_ATTEMPTS):
        delay = 2 ** attempt
        try:
            with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                payload = response.read(MAX_FEED_BYTES + 1)
            if len(payload) > MAX_FEED_BYTES:
                raise FeedError(f"Shelf '{shelf}' exceeded the feed size limit.")
            return parse_books(payload, shelf)
        except HTTPError as exc:
            status = exc.code
            retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
            exc.close()
            if status in {404, 410}:
                raise FeedUnavailable(
                    f"Goodreads RSS returned HTTP {status} for shelf '{shelf}'. "
                    "Check the Goodreads RSS service, user ID and shelf visibility; "
                    "this response does not establish why the service is unavailable."
                ) from exc
            if status not in RETRYABLE_STATUS:
                raise FeedError(f"Goodreads returned HTTP {status} for shelf '{shelf}'.") from exc
            reason = f"HTTP {status}"
            if retry_after.isdigit():
                delay = min(max(delay, int(retry_after)), 30)
        except (URLError, TimeoutError) as exc:
            reason = type(exc).__name__
        if attempt + 1 < MAX_ATTEMPTS:
            print(f"Retrying shelf '{shelf}' after {reason} ({attempt + 1}/{MAX_ATTEMPTS}).")
            time.sleep(delay)
    raise FeedUnavailable(
        f"Goodreads shelf '{shelf}' is unavailable after {MAX_ATTEMPTS} attempts ({reason})."
    )


def parse_books(payload, shelf):
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise FeedError(f"Shelf '{shelf}' did not return valid RSS XML.") from exc
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise FeedError(f"Shelf '{shelf}' did not return an RSS channel (possibly a sign-in page).")
    books = []
    for item in channel.findall("item"):
        book = {
            key: (item.findtext(tag) or "").strip()
            for key, tag in {
                "title": "title", "url": "link", "author": "author_name",
                "avg": "average_rating", "my_rating": "user_rating",
            }.items()
        }
        try:
            url = urlsplit(book["url"])
            valid_url = (
                url.scheme == "https"
                and url.hostname in {"www.goodreads.com", "goodreads.com"}
                and not url.username and not url.password and url.port in {None, 443}
            )
        except ValueError:
            valid_url = False
        if not book["title"] or not book["author"] or not valid_url:
            raise FeedError(f"Shelf '{shelf}' contains an incomplete or invalid book entry.")
        books.append(book)
    return books


def markdown_text(value):
    value = " ".join(value.split())
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\\x60*\[\]_])", r"\\\1", value)


def render_books(books, shelf):
    lines = []
    for book in books:
        title, author = markdown_text(book["title"]), markdown_text(book["author"])
        url = quote(book["url"], safe=":/?&=%#@+,-._~")
        average = markdown_text(book["avg"]) or "N/A"
        rating = f"Average Rating: {average}"
        if shelf == "read":
            rating = f"My Rating/Average Rating: {markdown_text(book['my_rating']) or 'N/A'}/{average}"
        lines.append(f"- [{title}]({url}) by {author}. {rating}")
    return "\n".join(lines)


def section_bounds(readme, tag):
    start, end = f"<!-- {tag}:START -->", f"<!-- {tag}:END -->"
    if readme.count(start) != 1 or readme.count(end) != 1:
        raise FeedError(f"README must contain exactly one START/END pair for {tag}.")
    left, right = readme.index(start) + len(start), readme.index(end)
    if left > right:
        raise FeedError(f"README markers are reversed for {tag}.")
    return left, right


def render_readme(readme, shelves):
    bounds = {shelf: section_bounds(readme, tag) for shelf, tag in SHELVES.items()}
    ordered = sorted(bounds.values())
    if any(left[1] >= right[0] for left, right in zip(ordered, ordered[1:])):
        raise FeedError("Goodreads sections must not overlap.")
    for shelf, (start, end) in sorted(bounds.items(), key=lambda item: item[1][0], reverse=True):
        readme = readme[:start] + "\n" + render_books(shelves[shelf], shelf) + "\n" + readme[end:]
    return readme


def atomic_write(path, text):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        temporary.chmod(path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def update_readme(path, user_id, dry_run=False):
    if not re.fullmatch(r"[0-9]+", user_id):
        raise FeedError("GOODREADS_USER_ID must be a numeric Goodreads user ID.")
    original = path.read_bytes().decode("utf-8")
    # Validate all markers before contacting Goodreads, even when its feed is down.
    render_readme(original, {shelf: [] for shelf in SHELVES})
    shelves = {shelf: fetch_shelf(user_id, shelf) for shelf in SHELVES}
    updated = render_readme(original, shelves)
    if updated == original:
        return "unchanged"
    if dry_run:
        return "would-update"
    atomic_write(path, updated)
    return "updated"


def report(status, message):
    print(message)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write(f"status={status}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(f"### Goodreads refresh: {status}\n\n{message}\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readme", type=Path, default=Path("README.md"))
    parser.add_argument("--dry-run", action="store_true", help="Validate live feeds without writing README.")
    args = parser.parse_args(argv)
    try:
        status = update_readme(args.readme, os.environ.get("GOODREADS_USER_ID", ""), args.dry_run)
    except FeedUnavailable as exc:
        message = f"{exc} Refresh skipped; README is unchanged and its book lists may be stale."
        report("unavailable", message)
        annotation = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::warning title=Goodreads refresh skipped::{annotation}")
        # Distinguish degraded operation from a successful refresh and from code errors.
        return 3
    except (FeedError, OSError, UnicodeError) as exc:
        report("failed", f"Goodreads refresh failed: {exc}")
        return 1
    messages = {
        "updated": "All three Goodreads shelves validated; README updated.",
        "unchanged": "All three Goodreads shelves validated; README is already current.",
        "would-update": "All three Goodreads shelves validated; dry run would update README.",
    }
    report(status, messages[status])
    return 0


if __name__ == "__main__":
    sys.exit(main())
