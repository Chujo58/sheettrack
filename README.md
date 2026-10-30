# sheettrack

Lightweight Google Sheets helpers for tracking pipeline/job status, one row per ID.

Your sheet needs an ID in column A and headers in row 1 (e.g. `EID`, `Status`, `Runtime`).

## Install

```bash
pip install sheettrack            # core
pip install "sheettrack[pandas]"  # + DataFrame support
```

## Setup

1. Create a Google Cloud service account and download its JSON key.
2. Enable the Google Sheets API for the project.
3. Share your spreadsheet with the service account's email (Editor).

## Usage

```python
from sheettrack import SheetClient

client = SheetClient(
    spreadsheet_id="your-spreadsheet-id",
    sheet_tab="Sheet1",
    credentials_file="google_service_account.json",  # or set GOOGLE_APPLICATION_CREDENTIALS
)

client.append_row({"EID": "123456789", "Status": "Pending"})
client.update_row("123456789", {"Status": "Complete", "Runtime": "2.5 hours"})
client.row_exists("123456789")                 # True
client.get_info("123456789")                   # {'EID': ..., 'Status': ...}
client.set_status(["111", "222"], "In batch processing")
df = client.get_dataframe()                    # needs sheettrack[pandas]

client.get_rows_by_fill_color((255, 0, 0))     # IDs of red-highlighted rows
client.get_rows_by_strikethrough()             # IDs of struck-through rows
```

Transient API errors (429/500/503) are retried with exponential backoff.

## License

MIT
