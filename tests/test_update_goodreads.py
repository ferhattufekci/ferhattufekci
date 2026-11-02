"""Offline regressions for Goodreads failures and README preservation."""

from contextlib import redirect_stdout
from email.message import Message
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from scripts import update_goodreads as updater


BOOK = {
    "title": "Türkçe Kitap",
    "url": "https://www.goodreads.com/review/show/42?utm_source=rss",
    "author": "Bir Yazar",
    "avg": "4.25",
    "my_rating": "5",
}
RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Books</title><item>
<title>Test Book</title><link>https://www.goodreads.com/review/show/42</link>
<author_name>Author</author_name><average_rating>4.25</average_rating>
<user_rating>5</user_rating></item></channel></rss>"""


def readme_fixture():
    return "PROFILE\r\n" + "\nOUTSIDE\n".join(
        f"<!-- {tag}:START -->\nold {shelf}\n<!-- {tag}:END -->"
        for shelf, tag in reversed(updater.SHELVES.items())
    ) + "\nFOOTER\r\n"


def http_error(status, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError("https://www.goodreads.com/feed", status, "test response", headers, None)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.open_patch = patch.object(updater, "urlopen")
        self.urlopen = self.open_patch.start()
        self.addCleanup(self.open_patch.stop)
        self.sleep_patch = patch.object(updater.time, "sleep")
        self.sleep = self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)
        self.stdout = redirect_stdout(io.StringIO())
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)

    def test_valid_rss(self):
        self.urlopen.return_value = io.BytesIO(RSS)
        books = updater.fetch_shelf("156149291", "currently-reading")
        self.assertEqual(books[0]["title"], "Test Book")
        request = self.urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://www.goodreads.com/review/list_rss/156149291?shelf=currently-reading&per_page=200",
        )
        self.assertEqual(self.urlopen.call_args.kwargs["timeout"], 15)

    def test_404_is_unavailable_and_is_not_retried(self):
        self.urlopen.side_effect = http_error(404)
        with self.assertRaisesRegex(updater.FeedUnavailable, "HTTP 404"):
            updater.fetch_shelf("156149291", "currently-reading")
        self.assertEqual(self.urlopen.call_count, 1)
        self.sleep.assert_not_called()

    def test_410_is_unavailable(self):
        self.urlopen.side_effect = http_error(410)
        with self.assertRaises(updater.FeedUnavailable):
            updater.fetch_shelf("156149291", "read")

    def test_403_is_a_failure_not_a_successful_skip(self):
        self.urlopen.side_effect = http_error(403)
        with self.assertRaises(updater.FeedError) as caught:
            updater.fetch_shelf("156149291", "read")
        self.assertNotIsInstance(caught.exception, updater.FeedUnavailable)
        self.sleep.assert_not_called()

    def test_503_then_valid_rss_recovers(self):
        self.urlopen.side_effect = [http_error(503), io.BytesIO(RSS)]
        self.assertEqual(len(updater.fetch_shelf("1", "read")), 1)
        self.sleep.assert_called_once_with(1)

    def test_429_respects_bounded_retry_after(self):
        self.urlopen.side_effect = [http_error(429, "999999"), io.BytesIO(RSS)]
        updater.fetch_shelf("1", "read")
        self.sleep.assert_called_once_with(30)

    def test_network_errors_and_server_errors_stop_after_three_attempts(self):
        for error in (TimeoutError(), URLError("DNS"), http_error(502)):
            with self.subTest(error=type(error).__name__):
                self.urlopen.reset_mock(side_effect=True)
                self.sleep.reset_mock()
                self.urlopen.side_effect = error
                with self.assertRaises(updater.FeedUnavailable):
                    updater.fetch_shelf("1", "read")
                self.assertEqual(self.urlopen.call_count, 3)
                self.assertEqual(self.sleep.call_count, 2)

    def test_oversized_feed_is_rejected(self):
        self.urlopen.return_value = io.BytesIO(b"x" * 11)
        with patch.object(updater, "MAX_FEED_BYTES", 10):
            with self.assertRaisesRegex(updater.FeedError, "size limit"):
                updater.fetch_shelf("1", "read")

    def test_invalid_xml_or_html_is_not_an_empty_shelf(self):
        for payload in (b"<rss", b"<html>Sign in</html>", b"<rss/>"):
            with self.subTest(payload=payload):
                with self.assertRaises(updater.FeedError):
                    updater.parse_books(payload, "read")

    def test_valid_empty_rss_is_an_empty_shelf(self):
        self.assertEqual(updater.parse_books(b"<rss><channel/></rss>", "read"), [])

    def test_incomplete_book_is_rejected(self):
        with self.assertRaises(updater.FeedError):
            updater.parse_books(RSS.replace(b"<title>Test Book</title>", b""), "read")

    def test_unsafe_or_invalid_link_is_rejected(self):
        for url in (
            b"javascript:alert(1)", b"https://example.com/book",
            b"https://goodreads.com.evil.example/book",
            b"https://user:password@goodreads.com/book", b"https://[bad",
        ):
            with self.subTest(url=url):
                with self.assertRaises(updater.FeedError):
                    updater.parse_books(
                        RSS.replace(b"https://www.goodreads.com/review/show/42", url), "read"
                    )


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "README.md"
        self.original = readme_fixture().encode("utf-8")
        self.path.write_bytes(self.original)

    def test_second_shelf_unavailable_keeps_entire_readme_byte_for_byte(self):
        with patch.object(updater, "fetch_shelf", side_effect=[
            [BOOK], updater.FeedUnavailable("HTTP 404"),
        ]):
            with self.assertRaises(updater.FeedUnavailable):
                updater.update_readme(self.path, "156149291")
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_invalid_second_shelf_keeps_entire_readme(self):
        with patch.object(updater, "fetch_shelf", side_effect=[
            [BOOK], updater.FeedError("invalid RSS"),
        ]):
            with self.assertRaises(updater.FeedError):
                updater.update_readme(self.path, "156149291")
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_success_preserves_outside_content_and_is_idempotent(self):
        with patch.object(updater, "fetch_shelf", return_value=[BOOK]) as fetch:
            self.assertEqual(updater.update_readme(self.path, "1"), "updated")
            changed = self.path.read_bytes()
            self.assertTrue(changed.startswith(b"PROFILE\r\n"))
            self.assertTrue(changed.endswith(b"FOOTER\r\n"))
            self.assertEqual(changed.count(b"\nOUTSIDE\n"), 2)
            self.assertIn("Türkçe Kitap".encode(), changed)
            self.assertIn(b"My Rating/Average Rating: 5/4.25", changed)
            self.assertEqual(fetch.call_count, 3)
            with patch.object(updater, "atomic_write") as write:
                self.assertEqual(updater.update_readme(self.path, "1"), "unchanged")
                write.assert_not_called()

    def test_dry_run_does_not_write(self):
        with patch.object(updater, "fetch_shelf", return_value=[BOOK]):
            self.assertEqual(updater.update_readme(self.path, "1", dry_run=True), "would-update")
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_invalid_markers_fail_before_network(self):
        readme = self.original.decode()
        start = "<!-- GOODREADS-LIST:START -->"
        end = "<!-- GOODREADS-LIST:END -->"
        invalid = [
            readme.replace(start, ""),
            readme + start,
            readme.replace(start, "TEMP").replace(end, start).replace("TEMP", end),
            readme.replace(end, "").replace(
                "<!-- GOODREADS-READ-LIST:END -->",
                end + "<!-- GOODREADS-READ-LIST:END -->",
            ),
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.path.write_bytes(value.encode())
                with patch.object(updater, "fetch_shelf") as fetch:
                    with self.assertRaises(updater.FeedError):
                        updater.update_readme(self.path, "1")
                    fetch.assert_not_called()
                self.assertEqual(self.path.read_bytes(), value.encode())

    def test_invalid_user_id_fails_before_network(self):
        for user_id in ("", "../1", "1?shelf=read", "username"):
            with self.subTest(user_id=user_id), patch.object(updater, "fetch_shelf") as fetch:
                with self.assertRaises(updater.FeedError):
                    updater.update_readme(self.path, user_id)
                fetch.assert_not_called()

    def test_failed_atomic_replace_keeps_original_and_cleans_temporary_file(self):
        with patch.object(updater.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                updater.atomic_write(self.path, "new text")
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_markdown_and_backslashes_are_literal(self):
        book = dict(BOOK, title="A [book] " + chr(92) + "1 <tag>\nnext", author="An *Author*")
        rendered = updater.render_books([book], "currently-reading")
        self.assertIn(r"A \[book\] \\1 &lt;tag&gt; next", rendered)
        self.assertIn(r"An \*Author\*", rendered)
        self.assertEqual(rendered.count("\n"), 0)


class CliTests(unittest.TestCase):
    def test_exit_code_and_github_outputs_distinguish_unavailable_from_success(self):
        cases = [
            ("updated", 0, None), ("unchanged", 0, None),
            ("unavailable", 3, updater.FeedUnavailable("HTTP 404")),
            ("failed", 1, updater.FeedError("invalid data")),
        ]
        for status, expected_code, error in cases:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as folder:
                output, summary = Path(folder) / "output", Path(folder) / "summary"
                env = {
                    "GOODREADS_USER_ID": "156149291",
                    "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary),
                }
                with patch.dict(os.environ, env, clear=True), patch.object(
                    updater, "update_readme", return_value=status, side_effect=error
                ), redirect_stdout(io.StringIO()) as stdout:
                    self.assertEqual(updater.main([]), expected_code)
                self.assertEqual(output.read_text(), f"status={status}\n")
                self.assertIn(f"Goodreads refresh: {status}", summary.read_text())
                if status == "unavailable":
                    self.assertIn("may be stale", summary.read_text())
                    self.assertIn("::warning", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
