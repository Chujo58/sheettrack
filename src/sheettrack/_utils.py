"""Small internal helpers (no network, easy to test)."""

from __future__ import annotations

import time

from googleapiclient.errors import HttpError

RETRYABLE_STATUSES = (429, 500, 503)


def execute_with_backoff(request, max_retries: int = 5, base_delay: float = 1.0):
    """Execute a Google API request, retrying transient errors with exponential backoff.

    Retries on HTTP 429/500/503. Other errors, or exhausting ``max_retries``,
    re-raise the original :class:`HttpError`.
    """
    for attempt in range(max_retries + 1):
        try:
            return request.execute()
        except HttpError as err:
            status = getattr(err.resp, "status", None)
            if status not in RETRYABLE_STATUSES or attempt == max_retries:
                raise
            delay = base_delay * (2**attempt)
            print(
                f"Google Sheets request failed with status {status}; retrying in {delay:.1f}s"
            )
            time.sleep(delay)


def column_letter(index: int) -> str:
    """Convert a 0-based column index to A1-notation letters (0 -> 'A', 26 -> 'AA')."""
    if index < 0:
        raise ValueError("Column index must be non-negative")
    label = ""
    current = index
    while True:
        current, remainder = divmod(current, 26)
        label = chr(65 + remainder) + label
        if current == 0:
            break
        current -= 1
    return label


def quote_tab(tab: str) -> str:
    """Quote a tab name for A1 notation, escaping embedded single quotes."""
    return "'" + tab.replace("'", "''") + "'"
