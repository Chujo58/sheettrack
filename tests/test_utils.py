import pytest
from googleapiclient.errors import HttpError

from sheettrack import SheetClient
from sheettrack._utils import column_letter, execute_with_backoff, quote_tab


@pytest.mark.parametrize("i,expected", [(0, "A"), (25, "Z"), (26, "AA"), (51, "AZ"), (52, "BA"), (701, "ZZ"), (702, "AAA")])
def test_column_letter(i, expected):
    assert column_letter(i) == expected


def test_column_letter_negative():
    with pytest.raises(ValueError):
        column_letter(-1)


def test_quote_tab():
    assert quote_tab("Sheet1") == "'Sheet1'"
    assert quote_tab("Bob's tab") == "'Bob''s tab'"


class _Resp:
    def __init__(self, status):
        self.status = status
        self.reason = "x"


class _Req:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    def execute(self):
        self.calls += 1
        s = self.statuses.pop(0)
        if s == 200:
            return {"ok": True}
        raise HttpError(_Resp(s), b"err")


def test_backoff_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    req = _Req([429, 503, 200])
    assert execute_with_backoff(req) == {"ok": True}
    assert req.calls == 3


def test_backoff_raises_nonretryable(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    req = _Req([404])
    with pytest.raises(HttpError):
        execute_with_backoff(req)
    assert req.calls == 1


def test_no_credentials_raises(monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    with pytest.raises(ValueError):
        SheetClient("abc").service


class FakeClient(SheetClient):
    """SheetClient with the network layer stubbed out."""

    def __init__(self, rows):
        super().__init__("sid")
        self.rows = rows
        self.updates = []

    def _values_get(self, a1_range):
        if a1_range.endswith("!1:1"):
            return [self.rows[0]]
        if a1_range.endswith("!A2:A"):
            return [[r[0]] if r else [] for r in self.rows[1:]]
        return self.rows

    def _batch_update_values(self, data):
        self.updates.extend(data)


def test_update_row_and_lookup():
    c = FakeClient([["EID", "Status", "Runtime"], ["1", "Pending"], [], ["2", "Done", "5"]])
    c.update_row("2", {"Status": "X", "Runtime": "9", "Nope": "ignored"})
    assert c.updates == [
        {"range": "'Sheet1'!B4", "values": [["X"]]},
        {"range": "'Sheet1'!C4", "values": [["9"]]},
    ]
    assert c.row_exists("1") and not c.row_exists("3")
    with pytest.raises(ValueError):
        c.update_row("3", {"Status": "X"})


def test_identifier_matching():
    c = FakeClient([["EID", "Host"], ["1", "a"], ["2", "a"], ["3", "b"]])
    assert c.get_info_by_identifier({"Host": "b"}) == {"EID": "3", "Host": "b"}
    with pytest.raises(ValueError):
        c.get_info_by_identifier({"Host": "a"})
    with pytest.raises(KeyError):
        c.get_info_by_identifier({"Host": "zzz"})
    c.update_row_by_identifier({"Host": "b"}, {"EID": "33"})
    assert c.updates == [{"range": "'Sheet1'!A4", "values": [["33"]]}]


def test_set_status_skips_missing(capsys):
    c = FakeClient([["EID", "Status"], ["1", ""], ["2", ""]])
    c.set_status(["1", "99"])
    assert c.updates == [{"range": "'Sheet1'!B2", "values": [["In batch processing"]]}]
    assert "99" in capsys.readouterr().out
