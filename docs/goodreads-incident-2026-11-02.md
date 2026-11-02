# Goodreads refresh failure — 2 November 2026

## Evidence and root cause

- Failed run: [Goodreads #60](https://github.com/ferhattufekci/ferhattufekci/actions/runs/41453379199), commit `adbede96f8187c71838448078da3878bd9506cdb`, at 16:29 Türkiye time (UTC+03).
- The Python update step raised `requests.exceptions.HTTPError: 404 Client Error` for `https://www.goodreads.com/review/list_rss/156149291?shelf=currently-reading&per_page=200`. The commit step was skipped.
- [Run #59](https://github.com/ferhattufekci/ferhattufekci/actions/runs/40312571959), on 26 October, successfully fetched the shelves with the same URL construction.
- Live checks on 2 November reproduced HTTP 404 for all three configured shelves, a request without `per_page`, the profile-slug URL variant, and the same RSS route for public user ID `1`. The owner's public profile still responded with HTTP 200. An attempted `format=rss` shelf URL redirected to sign-in and is not a valid replacement feed.
- Confirmed trigger: the Goodreads RSS source returned HTTP 404. Application-level cause of the failed job: the optional external feed's HTTP error escaped the inline script without an explicit unavailable outcome.
- The provider's internal reason is not confirmed. These observations suggest a shared RSS-route availability problem, but do not prove permanent removal, a particular privacy change, or an invalid account ID.
- The Node.js 20 deprecation warning is separate: checkout, Python setup and dependency installation all succeeded before the HTTP error. Ubuntu 26 was also used in the previous successful run. Neither warning is evidence for the 404's cause.

The old script already wrote README only after all three fetches, so this incident did not erase the previous book lists. The fix preserves that behavior and makes the degraded outcome explicit.

## Change and failure policy

The updater is a standalone Python standard-library script, with no pip installation required. It validates all README marker pairs before network access, validates RSS and book fields, renders all three shelves in memory, and atomically replaces README only after all shelves succeed. Sections outside the Goodreads markers remain untouched. The existing 200-items-per-shelf request limit and weekly schedule remain in place.

| Condition | Script result | Workflow behavior |
| --- | --- | --- |
| Valid feeds with changed content | `updated`, exit 0 | Commit README |
| Valid feeds with identical content | `unchanged`, exit 0 | No commit |
| HTTP 404/410 | `unavailable`, exit 3 | Preserve snapshot, emit warning and step summary, skip commit |
| Timeout/connection failure or HTTP 429/500/502/503/504 | Up to 3 attempts; then `unavailable`, exit 3 | Same explicit degraded outcome |
| Other HTTP errors, malformed RSS, invalid book data or README markers, file errors | Failure, exit 1 | Fail job; do not commit |

Only the dedicated unavailable exit code is accepted by the workflow. The CLI uses a distinct code from argparse's usage errors. A green workflow with `unavailable` means the old snapshot was preserved, **not** that the data was refreshed. The step summary explicitly warns that book lists may be stale. No unconditional `continue-on-error`, fallback empty feed, alternate account, or fabricated books are used.

The workflow now uses commit-pinned, Node.js 24-based checkout v7.0.1 and setup-python v7.1.0, Python 3.12, and Ubuntu 24.04. Tests run with read-only permissions on pull requests and relevant pushes. Only scheduled/manual execution on the default branch may update README; that job receives `contents: write`. Concurrent runs for the same ref are serialized. Push failures remain visible.

## Verification

- `python -m unittest discover -s tests -v`: 21 offline tests passed, including 404, transient recovery/exhaustion, rate limiting, invalid feeds and links, invalid markers, partial-fetch failure, atomic-write failure, idempotency, Markdown escaping and distinct CLI outcomes.
- Workflow YAML parsed successfully; all shell blocks passed `bash -n`. The actual refresh shell block was checked with exits 0, 1, 2 and 3: only 0 and 3 are accepted.
- `GOODREADS_USER_ID=156149291 python scripts/update_goodreads.py --dry-run`: live HTTP 404, exit 3, `status=unavailable`, warning and summary emitted; README SHA-256 was unchanged.
- `git diff --check`: passed.

## Remaining external dependency

This change hardens the integration; it does not restore Goodreads' RSS endpoint. A successful fresh-data update cannot be claimed while that endpoint returns 404. Once Goodreads recovers, rerun the workflow from the default branch and require `updated` or `unchanged` in the step summary. If the endpoint remains unavailable, obtain a currently supported feed/export mechanism from Goodreads before changing the data source. Do not interpret a login page or an empty fallback as a valid shelf.
