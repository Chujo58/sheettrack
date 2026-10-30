"""Google Sheets client for row-per-ID status tracking.

The sheet is expected to have an ID (e.g. an event ID) in the first column and
arbitrary metadata columns (Status, Runtime, ...) after it, with headers in row 1.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

from ._utils import column_letter, execute_with_backoff, quote_tab

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
BATCH_SIZE = 200


class SheetClient:
    """Read/update a Google Sheet where each row is keyed by an ID in column A.

    Parameters
    ----------
    spreadsheet_id : str
        The ID from the spreadsheet URL.
    sheet_tab : str
        Default tab name. Default ``"Sheet1"``.
    credentials_file : str, optional
        Path to a service-account JSON key. Falls back to the
        ``GOOGLE_APPLICATION_CREDENTIALS`` environment variable.
    credentials : google.auth.credentials.Credentials, optional
        Pre-built credentials (takes priority over ``credentials_file``).

    Examples
    --------
    >>> client = SheetClient("your-spreadsheet-id", credentials_file="key.json")
    >>> client.update_row("123456789", {"Status": "Complete", "Runtime": "2.5 hours"})
    """

    def __init__(
        self,
        spreadsheet_id: str,
        sheet_tab: str = "Sheet1",
        credentials_file: str | None = None,
        credentials: Any = None,
    ):
        self.spreadsheet_id = spreadsheet_id
        self.sheet_tab = sheet_tab
        self._credentials_file = credentials_file
        self._credentials = credentials
        self._service = None

    # ------------------------------------------------------------------ setup
    @property
    def service(self):
        """Lazily build the Sheets API service on first use."""
        if self._service is None:
            from google.oauth2.service_account import Credentials
            from googleapiclient.discovery import build

            creds = self._credentials
            if creds is None:
                path = self._credentials_file or os.environ.get(
                    "GOOGLE_APPLICATION_CREDENTIALS"
                )
                if not path:
                    raise ValueError(
                        "No credentials provided. Pass credentials_file=..., credentials=..., "
                        "or set GOOGLE_APPLICATION_CREDENTIALS."
                    )
                creds = Credentials.from_service_account_file(path, scopes=SCOPES)
            self._service = build(
                "sheets", "v4", credentials=creds, cache_discovery=False
            )
        return self._service

    def _tab(self, sheet_tab: str | None) -> str:
        return sheet_tab or self.sheet_tab

    def _values_get(self, a1_range: str) -> list[list]:
        req = (
            self.service.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range=a1_range)
        )
        return execute_with_backoff(req).get("values", [])

    def _batch_update_values(self, data: list[dict]) -> None:
        if not data:
            return
        req = (
            self.service.spreadsheets()
            .values()
            .batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "USER_ENTERED", "data": data},
            )
        )
        execute_with_backoff(req)

    # ------------------------------------------------------------------ reads
    def get_sheet_id_mapping(self) -> dict[str, int]:
        """Map tab titles to their numeric sheetIds."""
        meta = execute_with_backoff(
            self.service.spreadsheets().get(spreadsheetId=self.spreadsheet_id)
        )
        return {
            s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]
        }

    def get_values(
        self, sheet_tab: str | None = None, column_range: str = "A:Z"
    ) -> tuple[list[str], list[list]]:
        """Return ``(ids, all_rows)``. ``all_rows[0]`` is the header row."""
        tab = quote_tab(self._tab(sheet_tab))
        rows = self._values_get(f"{tab}!{column_range}")
        ids = [r[0] for r in rows[1:] if r]
        return ids, rows

    def get_dataframe(
        self,
        sheet_tab: str | None = None,
        column_range: str = "A:Z",
        header_row: int = 1,
    ):
        """Return the sheet as a ``pandas.DataFrame`` (requires ``sheettrack[pandas]``).

        ``header_row`` is the 1-based row holding the column names.
        """
        try:
            import pandas
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "Install pandas support with: pip install 'sheettrack[pandas]'"
            ) from e
        if header_row < 1:
            raise ValueError("header_row must be >= 1")

        _, rows = self.get_values(sheet_tab, column_range)
        headers = rows[header_row - 1]
        body = [list(r) + [""] * (len(headers) - len(r)) for r in rows[header_row:]]
        return pandas.DataFrame([r[: len(headers)] for r in body], columns=headers)

    def get_event_row_map(self, sheet_tab: str | None = None) -> dict[str, int]:
        """Map each ID in column A to its 1-based sheet row number."""
        tab = quote_tab(self._tab(sheet_tab))
        values = self._values_get(f"{tab}!A2:A")
        row_map: dict[str, int] = {}
        for row_number, row in enumerate(values, start=2):
            if row and str(row[0]).strip():
                row_map[str(row[0]).strip()] = row_number
        return row_map

    def row_exists(
        self, eventid, prefetched: tuple | None = None, sheet_tab: str | None = None
    ) -> bool:
        """Check whether an ID exists. Pass ``prefetched=get_values()`` to avoid refetching."""
        eid = str(eventid).strip()
        if prefetched is not None:
            ids, _ = prefetched
            return any(str(i).strip() == eid for i in ids)
        return eid in self.get_event_row_map(sheet_tab)

    def get_column_keys(self, sheet_tab: str | None = None) -> list[str]:
        """Return the header row."""
        tab = quote_tab(self._tab(sheet_tab))
        rows = self._values_get(f"{tab}!1:1")
        return rows[0] if rows else []

    def get_column_map(self, sheet_tab: str | None = None) -> dict[str, int]:
        """Return ``{header: 0-based column index}``."""
        return {k: i for i, k in enumerate(self.get_column_keys(sheet_tab))}

    def _matching_rows(self, identifier: dict[str, Any], sheet_tab: str | None):
        """Return ``(headers, [(row_number, padded_row), ...])`` for rows matching all identifier fields."""
        if not identifier:
            raise ValueError("Identifier cannot be empty.")
        tab = quote_tab(self._tab(sheet_tab))
        rows = self._values_get(f"{tab}!A:ZZ")
        if not rows:
            raise ValueError("The spreadsheet is empty.")
        headers = rows[0]
        index = {k: i for i, k in enumerate(headers)}
        for key in identifier:
            if key not in index:
                raise KeyError(
                    f"Identifier column {key!r} does not exist in the sheet."
                )

        matches = []
        for row_number, row in enumerate(rows[1:], start=2):
            padded = list(row) + [""] * (len(headers) - len(row))
            if all(str(padded[index[k]]) == str(v) for k, v in identifier.items()):
                matches.append((row_number, padded))
        return headers, matches

    def get_info(self, eid, sheet_tab: str | None = None) -> dict:
        """Return a ``{header: value}`` dict for the row whose ID is ``eid``."""
        headers = self.get_column_keys(sheet_tab)
        row_map = self.get_event_row_map(sheet_tab)
        row_number = row_map.get(str(eid).strip())
        if row_number is None:
            raise KeyError(f"Cannot find the selected ID: {eid}")
        tab = quote_tab(self._tab(sheet_tab))
        rows = self._values_get(f"{tab}!A{row_number}:ZZ{row_number}")
        row = list(rows[0]) if rows else []
        row += [""] * (len(headers) - len(row))
        return dict(zip(headers, row))

    def get_info_by_identifier(
        self, identifier: dict[str, Any], sheet_tab: str | None = None
    ) -> dict:
        """Return a row dict where all ``identifier`` column/value pairs match.

        Raises ``KeyError`` if nothing matches, ``ValueError`` if several rows do.
        """
        headers, matches = self._matching_rows(identifier, sheet_tab)
        if not matches:
            raise KeyError(f"Cannot find a row matching identifier: {identifier}")
        if len(matches) > 1:
            raise ValueError(f"Multiple rows match identifier: {identifier}")
        return dict(zip(headers, matches[0][1]))

    # ----------------------------------------------------------------- writes
    def append_row(self, row_values: dict, sheet_tab: str | None = None) -> None:
        """Append a row, mapping dict keys to columns by header. Missing columns are blank."""
        tab = self._tab(sheet_tab)
        keys = self.get_column_keys(tab)
        new_row = [row_values.get(k, "") for k in keys]
        req = (
            self.service.spreadsheets()
            .values()
            .append(
                spreadsheetId=self.spreadsheet_id,
                range=quote_tab(tab),
                valueInputOption="USER_ENTERED",
                body={"values": [new_row]},
            )
        )
        execute_with_backoff(req)

    def _cell_updates(
        self, tab: str, row_number: int, col_map: dict[str, int], values: dict
    ) -> list[dict]:
        return [
            {
                "range": f"{quote_tab(tab)}!{column_letter(col_map[k])}{row_number}",
                "values": [[v]],
            }
            for k, v in values.items()
            if k in col_map
        ]

    def update_row(
        self, eventid, row_values: dict, sheet_tab: str | None = None
    ) -> None:
        """Update only the given columns of the row whose ID is ``eventid``.

        Raises ``ValueError`` if the ID is not in the sheet. Unknown column names are ignored.
        """
        if not row_values:
            return
        tab = self._tab(sheet_tab)
        col_map = self.get_column_map(tab)
        row_number = self.get_event_row_map(tab).get(str(eventid).strip())
        if row_number is None:
            raise ValueError(f"Event ID {eventid} does not exist in the spreadsheet.")
        self._batch_update_values(
            self._cell_updates(tab, row_number, col_map, row_values)
        )

    def update_row_by_identifier(
        self, identifier: dict[str, Any], row_values: dict, sheet_tab: str | None = None
    ) -> None:
        """Update the single row matching every ``identifier`` column/value pair."""
        if not row_values:
            return
        tab = self._tab(sheet_tab)
        headers, matches = self._matching_rows(identifier, tab)
        if not matches:
            raise ValueError(f"No row found matching identifier {identifier!r}.")
        if len(matches) > 1:
            raise ValueError(
                f"Identifier {identifier!r} matched multiple rows: {[m[0] for m in matches]}."
            )
        col_map = {k: i for i, k in enumerate(headers)}
        self._batch_update_values(
            self._cell_updates(tab, matches[0][0], col_map, row_values)
        )

    def set_status(
        self,
        eids: Iterable,
        status: str = "In batch processing",
        status_column: str = "Status",
        sheet_tab: str | None = None,
    ) -> None:
        """Set one column to the same value for many IDs, in batched API calls.

        IDs not found in the sheet are skipped with a printed warning.
        """
        tab = self._tab(sheet_tab)
        row_map = self.get_event_row_map(tab)
        col_map = self.get_column_map(tab)
        if status_column not in col_map:
            raise KeyError(
                f"Cannot find {status_column!r} column in spreadsheet headers."
            )
        letter = column_letter(col_map[status_column])

        data: list[dict] = []
        for eid in eids:
            row_number = row_map.get(str(eid).strip())
            if row_number is None:
                print(f"Event ID {eid} does not exist in the spreadsheet. Skipping...")
                continue
            data.append(
                {
                    "range": f"{quote_tab(tab)}!{letter}{row_number}",
                    "values": [[status]],
                }
            )
            if len(data) >= BATCH_SIZE:
                self._batch_update_values(data)
                data = []
        self._batch_update_values(data)

    def batch_rows(self, eids: Iterable, sheet_tab: str | None = None) -> None:
        """Mark IDs as ``"In batch processing"`` (shortcut for :meth:`set_status`)."""
        self.set_status(eids, sheet_tab=sheet_tab)

    # ------------------------------------------------------- formatting reads
    def _grid_formats(self, tab: str, fields: str) -> list[dict]:
        resp = execute_with_backoff(
            self.service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id,
                ranges=[f"{quote_tab(tab)}!A:Z"],
                includeGridData=True,
                fields=fields,
            )
        )
        sheets = resp.get("sheets", [])
        if not sheets:
            return []
        return sheets[0].get("data", [{}])[0].get("rowData", [])

    @staticmethod
    def _result_for(values: list[dict], row_number: int, check_column_index: int):
        if check_column_index == 0:
            return values[0].get("formattedValue", "") if values else ""
        return row_number

    def get_rows_by_fill_color(
        self,
        target_rgb: tuple[int, int, int],
        sheet_tab: str | None = None,
        check_column_index: int = 0,
        start_row: int = 2,
    ) -> list[str | int]:
        """Return IDs (column A) or row numbers for cells with the given background colour.

        Uses effective formatting, so conditional formatting is honoured. Slower than value reads.
        """
        row_data = self._grid_formats(
            self._tab(sheet_tab),
            "sheets(data(rowData(values(formattedValue,"
            "userEnteredFormat(backgroundColor,backgroundColorStyle),"
            "effectiveFormat(backgroundColor,backgroundColorStyle)))))",
        )

        def to_255(c: dict) -> tuple[int, int, int]:
            return tuple(round(c.get(k, 0.0) * 255) for k in ("red", "green", "blue"))

        matches: list[str | int] = []
        for i, row in enumerate(row_data, start=1):
            if i < start_row:
                continue
            values = row.get("values", [])
            if check_column_index >= len(values):
                continue
            fmt = values[check_column_index].get("effectiveFormat") or values[
                check_column_index
            ].get("userEnteredFormat", {})
            color = fmt.get("backgroundColor") or {}
            if color and to_255(color) == tuple(target_rgb):
                matches.append(self._result_for(values, i, check_column_index))
        return matches

    def get_rows_by_strikethrough(
        self,
        sheet_tab: str | None = None,
        check_column_index: int = 0,
        start_row: int = 2,
    ) -> list[str | int]:
        """Return IDs (column A) or row numbers for cells with strikethrough text."""
        row_data = self._grid_formats(
            self._tab(sheet_tab),
            "sheets(data(rowData(values(formattedValue,"
            "userEnteredFormat(textFormat(strikethrough)),"
            "effectiveFormat(textFormat(strikethrough))))))",
        )
        matches: list[str | int] = []
        for i, row in enumerate(row_data, start=1):
            if i < start_row:
                continue
            values = row.get("values", [])
            if check_column_index >= len(values):
                continue
            fmt = values[check_column_index].get("effectiveFormat") or values[
                check_column_index
            ].get("userEnteredFormat", {})
            if fmt.get("textFormat", {}).get("strikethrough", False):
                matches.append(self._result_for(values, i, check_column_index))
        return matches
