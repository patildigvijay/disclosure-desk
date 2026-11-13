import io
import types
import zipfile
from datetime import date

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from conftest import FIXTURES
import common
import house
import llm
import senate
from enrich import Members, Sectors, enrich, is_equity, load_committee_sectors
from validate import check


# ---------- shared fixtures ----------

@pytest.fixture(scope="module")
def members():
    return Members.load(folder=FIXTURES)


@pytest.fixture
def sectors():
    # Offline: injected SEC ticker list and SIC codes (real SIC codes for these companies).
    tickers = {"LMT": 936468, "NVDA": 1045810, "UNH": 731766, "AAPL": 320193, "SPY": 884394}
    sic = {"LMT": {"ticker": "LMT", "sic": "3760"}, "NVDA": {"ticker": "NVDA", "sic": "3674"},
           "UNH": {"ticker": "UNH", "sic": "6324"}, "AAPL": {"ticker": "AAPL", "sic": "3571"},
           "SPY": {"ticker": "SPY", "sic": "6726"}}
    return Sectors(tickers=tickers, sic_cache=sic)


# ---------- common ----------

@pytest.mark.parametrize("text,expected", [
    ("$1,001 - $15,000", (1001, 15000)),
    ("$15,001 -\n$50,000", (15001, 50000)),
    ("Over $50,000,000", (50000001, None)),
    ("Spouse/DC Over $1,000,000", (1000001, None)),
    ("", (None, None)),
])
def test_parse_amount(text, expected):
    assert common.parse_amount(text) == expected


def test_normalisers():
    assert common.norm_owner("SP") == "Spouse"
    assert common.norm_owner("") == "Self"
    assert common.norm_tx_type("S (partial)") == "Sale (Partial)"
    assert common.norm_tx_type("Sale (Full)") == "Sale"
    assert common.norm_ticker("--") == ""


# ---------- House ----------

def test_house_index_real_rows():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.write(FIXTURES / "house_index_sample.xml", "2026FD.xml")
    rows = house.parse_index(buf.getvalue())
    assert [r["filing_id"] for r in rows] == ["20034916", "20035691", "9116142"]  # PTRs only
    w = rows[0]
    assert (w["state"], w["district"], w["filing_date"], w["kind"]) == ("VA", "01", date(2026, 7, 10), "efile")
    assert w["source_url"] == "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20034916.pdf"
    assert rows[2]["kind"] == "paper"


def make_ptr_pdf():
    """Synthetic e-filed PTR laid out like the House form, incl. a wrapped asset and amount."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont("Helvetica", 7)
    t = lambda x, y, s: c.drawString(x, y, s)
    t(40, 740, "Periodic Transaction Report")
    t(40, 720, "Name: Hon. Robert J. Wittman")
    xs = dict(id=40, owner=60, asset=100, type=300, date=360, notif=420, amount=480, gains=560)
    for k, s in [("id", "ID"), ("owner", "Owner"), ("asset", "Asset"), ("type", "Transaction"),
                 ("date", "Date"), ("notif", "Notification"), ("amount", "Amount"), ("gains", "Cap.")]:
        t(xs[k], 680, s)
    t(xs["type"], 672, "Type"); t(xs["notif"], 672, "Date"); t(xs["gains"], 672, "Gains >")
    t(xs["gains"], 664, "$200?")
    # Row 1
    t(xs["owner"], 640, "SP"); t(xs["asset"], 640, "Apple Inc. - Common Stock (AAPL) [ST]")
    t(xs["type"], 640, "P"); t(xs["date"], 640, "01/02/2026"); t(xs["notif"], 640, "01/05/2026")
    t(xs["amount"], 640, "$1,001 - $15,000")
    t(xs["asset"], 630, "F S : New")
    # Row 2: wrapped asset name and amount
    t(xs["asset"], 610, "Lockheed Martin Corporation"); t(xs["type"], 610, "S (partial)")
    t(xs["date"], 610, "02/10/2026"); t(xs["notif"], 610, "02/12/2026"); t(xs["amount"], 610, "$15,001 -")
    t(xs["asset"], 600, "Common Stock (LMT) [ST]"); t(xs["amount"], 600, "$50,000")
    t(xs["asset"], 590, "F S : New"); t(xs["asset"], 580, "S O : Brokerage")
    t(xs["asset"], 570, "D : Sold half")
    t(40, 540, "* For the complete list of asset type abbreviations, please visit https://fd.house.gov/reference/asset-type-codes.aspx.")
    t(40, 520, "I P O")
    t(40, 500, "Digitally Signed: Hon. Robert J. Wittman , 02/13/2026")
    c.save()
    return buf.getvalue()


def test_house_pdf_parser_synthetic_layout():
    rows, expected = house.parse_pdf(make_ptr_pdf())
    assert expected == 2 and len(rows) == 2
    a, l = rows
    assert (a["owner"], a["ticker"], a["asset_type"], a["tx_type"]) == ("Spouse", "AAPL", "Stock", "Purchase")
    assert (a["tx_date"], a["notification_date"]) == (date(2026, 1, 2), date(2026, 1, 5))
    assert (a["amount_min"], a["amount_max"]) == (1001, 15000)
    assert a["asset_name"] == "Apple Inc. - Common Stock"
    assert (l["owner"], l["ticker"], l["tx_type"]) == ("Self", "LMT", "Sale (Partial)")
    assert l["asset_name"] == "Lockheed Martin Corporation Common Stock"
    assert (l["amount_min"], l["amount_max"]) == (15001, 50000)
    assert l["comment"] == "SO: Brokerage; D: Sold half"


REAL = FIXTURES / "house_real"  # filled from the user's sample zip; see README


@pytest.mark.skipif(not (REAL / "expected.json").exists(), reason="real House samples not added yet")
def test_house_pdf_parser_real_samples():
    import json
    expected = json.loads((REAL / "expected.json").read_text())
    for doc_id, want in expected.items():
        rows, n = house.parse_pdf((REAL / f"{doc_id}.pdf").read_bytes())
        assert len(rows) == n == len(want), doc_id
        for got, w in zip(rows, want):
            for k, v in w.items():
                g = got[k].isoformat() if hasattr(got[k], "isoformat") else got[k]
                assert g == v, (doc_id, k, g, v)


# ---------- Senate ----------

def test_senate_search_row():
    row = ["Tommy", "Tuberville", "Tuberville, Tommy (Senator)",
           '<a href="/search/view/ptr/0b1c2d3e-aaaa-bbbb-cccc-1234567890ab/" target="_blank">Periodic Transaction Report for 10/02/2026</a>',
           "10/02/2026"]
    f = senate.parse_search_row(row)
    assert f["filing_id"] == "0b1c2d3e-aaaa-bbbb-cccc-1234567890ab"
    assert f["kind"] == "efile" and f["filing_date"] == date(2026, 10, 2)
    paper = senate.parse_search_row(row[:3] + ['<a href="/search/view/paper/ABC-123/">x</a>', "01/01/2026"])
    assert paper["kind"] == "paper"


def test_senate_ptr_html_synthetic():
    rows = senate.parse_ptr_html((FIXTURES / "senate_ptr_synthetic.html").read_text())
    assert len(rows) == 3
    assert (rows[0]["ticker"], rows[0]["tx_type"], rows[0]["amount_min"]) == ("LMT", "Purchase", 15001)
    assert (rows[1]["owner"], rows[1]["tx_type"]) == ("Spouse", "Sale (Partial)")
    assert rows[2]["ticker"] == "" and rows[2]["comment"] == "Maturity 2031"


# ---------- members, sectors, overlap ----------

def test_member_matching_real_data(members):
    assert members.match("House", "Wittman", "Robert J.", "VA", "01")["bioguide"] == "W000804"
    assert members.match("House", "Yakym", "Rudy C.", "IN", "02")["bioguide"] == "Y000067"
    assert members.match("Senate", "Tuberville", "Tommy")["bioguide"] == "T000278"
    # Two senators named Scott: first name decides
    assert members.match("Senate", "Scott", "Rick")["bioguide"] == "S001217"
    assert members.match("Senate", "Scott", "Tim")["bioguide"] == "S001184"
    assert members.match("House", "Nobody", "X", "VA", "01") is None


def test_every_mapped_committee_exists(members):
    ids = {c for cs in members.committees.values() for c in cs}
    missing = set(load_committee_sectors()) - ids
    assert not missing, missing


@pytest.mark.parametrize("sic,sector", [
    ("3760", "Defense & Aerospace"), ("3721", "Defense & Aerospace"), ("3674", "Technology"),
    ("2834", "Health Care"), ("6022", "Financials"), ("6798", "Real Estate"), ("6726", "Fund"),
    ("1311", "Energy"), ("4911", "Utilities"), ("", ""),
])
def test_sic_to_sector(sectors, sic, sector):
    assert sectors.sector_for_sic(sic) == sector


def test_is_equity():
    assert is_equity("Stock") and is_equity("Option") and is_equity("Stock Option")
    assert not is_equity("ETF") and not is_equity("Non-Public Stock") and not is_equity("Corporate Bond")


def trade(**kw):
    t = dict(asset_name="X", ticker="", asset_type="Stock", tx_type="Purchase",
             tx_date=date(2026, 6, 1), notification_date=date(2026, 6, 2),
             amount_min=1001, amount_max=15000)
    t.update(kw)
    return t


def test_overlap_flag(members, sectors):
    cs = load_committee_sectors()
    wittman = {"chamber": "House", "last_name": "Wittman", "first_name": "Robert J.", "state": "VA", "district": "01"}
    t = enrich(trade(ticker="LMT"), wittman, members, cs, sectors)
    assert t["sector"] == "Defense & Aerospace" and t["overlap"] is True
    assert "House Armed Services" in t["overlap_committees"]
    t = enrich(trade(ticker="AAPL"), wittman, members, cs, sectors)
    assert t["sector"] == "Technology"
    # ETF never gets a sector, so never an overlap
    t = enrich(trade(ticker="SPY", asset_type="ETF"), wittman, members, cs, sectors)
    assert t["sector"] == "" and t["overlap"] is False
    tuberville = {"chamber": "Senate", "last_name": "Tuberville", "first_name": "Tommy"}
    t = enrich(trade(ticker="UNH"), tuberville, members, cs, sectors)
    assert t["sector"] == "Health Care" and t["overlap"] is True  # SIC 6324 = medical service plans
    assert "Senate Health, Education, Labor, and Pensions" in t["overlap_committees"]


# ---------- validation ----------

def test_validation_rules(sectors):
    ok = check(trade(ticker="AAPL"), date(2026, 6, 20), sectors)
    assert ok["status"] == "ok" and ok["issues"] == "" and ok["filing_lag_days"] == 19 and not ok["late"]
    late = check(trade(), date(2026, 8, 1))
    assert late["late"] and late["status"] == "ok"
    bad = check(trade(amount_min=2000, amount_max=9000, tx_date=date(2026, 9, 1)), date(2026, 8, 1))
    assert bad["status"] == "review"
    assert "amount_not_official" in bad["issues"] and "tx_after_filing" in bad["issues"]
    assert "low_confidence" in check(trade(), date(2026, 6, 20), confidence=0.6)["issues"]
    assert check(trade(ticker="ZZZZ"), date(2026, 6, 20), sectors)["status"] == "ok"  # warning only


# ---------- AI extractor (mocked: no API key here) ----------

def fake_client(content, stop="tool_use", calls=None, usage=(1000, 500)):
    """Stands in for anthropic.Anthropic(): messages.stream(...) -> final message."""
    msg = types.SimpleNamespace(content=content, stop_reason=stop,
                                usage=types.SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1]))

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return msg

    class FakeMessages:
        def stream(self, **kw):
            if calls is not None:
                calls.update(kw)
            return Stream()
    return types.SimpleNamespace(messages=FakeMessages())


def test_llm_extract_normalises_output():
    block = types.SimpleNamespace(type="tool_use", input={
        "transactions": [{"owner": "SP", "asset_name": "Microsoft Corp", "ticker": "msft",
                          "asset_type": "[ST]", "tx_type": "S (partial)", "tx_date": "03/04/2026",
                          "notification_date": "03/06/2026", "amount_range": "$50,001 - $100,000"}],
        "confidence": 0.97, "notes": ""})
    calls = {}
    rows, conf, _ = llm.extract_pdf(b"%PDF-1.4 fake", client=fake_client([block], calls=calls))
    assert conf == 0.97 and calls["model"] == llm.MODEL
    r = rows[0]
    assert (r["owner"], r["ticker"], r["asset_type"], r["tx_type"]) == ("Spouse", "MSFT", "Stock", "Sale (Partial)")
    assert (r["amount_min"], r["amount_max"], r["tx_date"]) == (50001, 100000, date(2026, 3, 4))


# ---------- end-to-end glue (network replaced by fixtures) ----------

def test_pipeline_end_to_end(tmp_path, monkeypatch, members, sectors):
    import csv
    import pipeline

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.write(FIXTURES / "house_index_sample.xml", "2026FD.xml")
    pdf = make_ptr_pdf()
    sectors.cache_path = tmp_path / "sic.csv"
    monkeypatch.setattr(pipeline, "DATA", tmp_path)
    monkeypatch.setattr(pipeline, "session", lambda: None)
    monkeypatch.setattr(pipeline.Members, "load", classmethod(lambda cls, s=None: members))
    monkeypatch.setattr(pipeline, "Sectors", lambda s: sectors)
    monkeypatch.setattr(pipeline.house, "fetch_index", lambda s, y: house.parse_index(buf.getvalue()) if y == 2026 else [])
    monkeypatch.setattr(pipeline, "get", lambda s, url: types.SimpleNamespace(content=pdf))
    monkeypatch.setattr(pipeline.llm, "available", lambda: False)
    monkeypatch.setattr(pipeline.prices, "load", lambda *a, **k: None)

    assert pipeline.run(date(2026, 1, 1), ["house"]) == 0
    trades = list(csv.DictReader(open(tmp_path / "trades.csv")))
    filings = {r["filing_id"]: r for r in csv.DictReader(open(tmp_path / "filings.csv"))}
    assert len(trades) == 4  # 2 e-filed PTRs x 2 rows
    assert filings["9116142"]["status"] == "needs_ai"  # paper scan, no API key
    assert filings["20034916"]["status"] == "parsed"
    lmt = next(t for t in trades if t["filing_id"] == "20034916" and t["ticker"] == "LMT")
    assert lmt["bioguide"] == "W000804" and lmt["overlap"] == "True" and lmt["parse_method"] == "text"
    assert lmt["amount_range"] == "$15,001 - $50,000"
    # Second run: nothing new to do, nothing duplicated
    assert pipeline.run(date(2026, 1, 1), ["house"]) == 0
    assert len(list(csv.DictReader(open(tmp_path / "trades.csv")))) == 4


def test_member_rows_for_website(members):
    rows = {r["bioguide"]: r for r in members.rows()}
    assert len(rows) == 539
    t = rows["T000278"]
    assert (t["chamber"], t["state"], t["party"]) == ("Senate", "AL", "Republican")
    assert "Senate Committee on Armed Services" in t["committees"]
    assert "Senate Majority Whip" in rows["B001261"]["leadership"]  # Barrasso
    w = rows["W000804"]
    assert w["district"] == 1 and "House Committee on Armed Services" in w["committees"]


# ---------- estimated holdings and performance ----------

import portfolio  # noqa: E402


def ptrade(tid, tk, tx, d, lo=15001, hi=50000, who="W000804", filed=None, overlap="False"):
    return {"trade_id": tid, "status": "ok", "bioguide": who, "filer_name": "X", "ticker": tk,
            "asset_type": "Stock", "asset_name": tk + " Inc", "tx_type": tx, "tx_date": d,
            "filing_date": filed or d, "amount_min": lo, "amount_max": hi, "overlap": overlap, "chamber": "House"}


def test_holdings_at_cost():
    trades = [
        ptrade("1", "LMT", "Purchase", "2026-01-05", overlap="True"),        # +32,500.5
        ptrade("2", "LMT", "Purchase", "2026-02-05", 1001, 15000),           # + 8,000.5
        ptrade("3", "LMT", "Sale (Partial)", "2026-03-05", 1001, 15000),     # - 8,000.5
        ptrade("4", "AAPL", "Purchase", "2026-01-10"),
        ptrade("5", "AAPL", "Sale", "2026-04-01"),                           # closed
        ptrade("6", "NVDA", "Purchase", "2026-05-01", 50001, 100000),
        {**ptrade("7", "NVDA", "Purchase", "2026-05-02"), "asset_type": "Option"},  # options ignored
    ]
    rows = {r["ticker"]: r for r in portfolio.holdings(trades)}
    assert set(rows) == {"LMT", "NVDA"}
    assert rows["LMT"]["est_value"] == 32500 and rows["LMT"]["last_action"] == "Reduce"
    assert rows["LMT"]["overlap"] is True and rows["NVDA"]["last_action"] == "Buy"
    assert round(rows["LMT"]["weight"] + rows["NVDA"]["weight"]) == 100
    assert rows["NVDA"]["valued_at"] == "cost"


def fake_prices():
    d = lambda s: date.fromisoformat(s)
    return portfolio.Prices({
        "LMT": [(d("2026-01-02"), 400.0), (d("2026-01-09"), 410.0), (d("2026-06-01"), 480.0)],
        "SPY": [(d("2026-01-02"), 500.0), (d("2026-01-09"), 505.0), (d("2026-06-01"), 550.0)],
    })


def test_prices_close_on_uses_last_trading_day():
    p = fake_prices()
    assert p.close_on("LMT", date(2026, 1, 4)) == 400.0      # weekend -> previous close
    assert p.close_on("LMT", date(2025, 12, 1)) is None      # before history
    assert p.latest("SPY") == (date(2026, 6, 1), 550.0)


def test_holdings_marked_to_market():
    rows = portfolio.holdings([ptrade("1", "LMT", "Purchase", "2026-01-02", 40000, 40000)], fake_prices())
    assert rows[0]["est_value"] == 48000 and rows[0]["valued_at"] == "2026-06-01"  # 100 shares x 480


def test_performance_vs_spy_and_copy():
    trades = [ptrade(str(i), "LMT", "Purchase", "2026-01-02", 40000, 40000, filed="2026-01-09") for i in range(3)]
    perf = portfolio.performance(trades, fake_prices(), date(2026, 6, 2))["W000804"]
    assert perf["buys_12m"] == 3
    assert perf["return_12m"] == 20.0 and perf["spy_12m"] == 10.0 and perf["excess_12m"] == 10.0
    assert perf["copy_return_12m"] == 17.1 and perf["copy_spy_12m"] == 8.9  # bought at filing date instead
    assert portfolio.performance(trades[:2], fake_prices(), date(2026, 6, 2)) == {}  # under MIN_BUYS
    assert portfolio.performance(trades, None, date(2026, 6, 2)) == {}


def test_member_key_matches_website_slug():
    assert portfolio.member_key({"bioguide": "", "filer_name": "José Luis O'Neil"}) == "x-jose-luis-o-neil"


def test_scanned_house_pdf_has_no_text_layer():
    assert house.text_layer((REAL / "9116249.pdf").read_bytes()).strip() == ""


def test_amount_exact_under_threshold_is_valid():
    assert common.parse_amount("$344.99") == (344.99, 344.99)
    assert common.amount_ok(344.99, 344.99) and not common.amount_ok(15001, 15001)
    assert common.amount_label(344.99, 344.99) == "$344.99"


# ---------- prices (Marketstack, network replaced by a fake) ----------

def test_prices_fetch_batches_pages_and_daily_cache(tmp_path, monkeypatch):
    import prices
    calls = []

    def fake_get(s, url, params):
        calls.append(params)
        syms = params["symbols"].split(",")
        rows = [{"symbol": sy, "date": f"2026-06-0{d}T00:00:00+0000", "adj_close": 100.0 + d, "close": 1.0}
                for sy in syms for d in (1, 2)]
        page = rows[params["offset"]:params["offset"] + 3]  # force pagination
        return types.SimpleNamespace(json=lambda: {"pagination": {"count": len(page), "total": len(rows)}, "data": page})

    monkeypatch.setattr(prices, "get", fake_get)
    monkeypatch.setattr(prices, "CACHE", tmp_path)
    monkeypatch.setattr(prices, "STATE", tmp_path / "_state.json")
    monkeypatch.delenv("MARKETSTACK_API_KEY", raising=False)
    monkeypatch.setenv("PRICE_SOURCE", "none")
    assert prices.load(None, ["LMT"], date(2026, 1, 1)) is None  # turned off: no performance
    monkeypatch.setenv("MARKETSTACK_API_KEY", "k")
    px = prices.load(None, ["LMT", "NVDA"], date(2026, 1, 1), today=date(2026, 6, 3))
    assert px.close_on("LMT", date(2026, 6, 3)) == 102.0 and px.latest("SPY")[1] == 102.0
    n = len(calls)
    assert n == 2  # 3 symbols x 2 days = 6 rows, 3 per page
    prices.load(None, ["LMT", "NVDA"], date(2026, 1, 1), today=date(2026, 6, 3))
    assert len(calls) == n  # already fetched today: served from cache


def test_holdings_track_each_owner_separately():
    trades = [
        {**ptrade("1", "AAPL", "Purchase", "2026-01-05"), "owner": "Spouse"},
        {**ptrade("2", "AAPL", "Purchase", "2026-01-06"), "owner": "Self"},
        {**ptrade("3", "AAPL", "Sale", "2026-02-01"), "owner": "Self"},   # closes Self only
    ]
    h = portfolio.holdings(trades)[0]
    assert h["held_by"] == "Spouse=100" and h["last_action"] == "Reduce" and h["est_value"] == 32500


def test_benchmark_12m():
    assert portfolio.benchmark_12m(fake_prices(), date(2027, 1, 2)) == 10.0  # 500 -> 550
    assert portfolio.benchmark_12m(None, date(2027, 1, 2)) is None


def test_yfinance_source_parses_download(tmp_path, monkeypatch):
    import pandas as pd
    import prices
    import yfinance
    idx = pd.to_datetime(["2026-06-01", "2026-06-02"])
    cols = pd.MultiIndex.from_product([["Close"], ["BRK-B", "SPY"]])
    df = pd.DataFrame([[400.0, 500.0], [410.0, 505.0]], index=idx, columns=cols)
    calls = []
    monkeypatch.setattr(yfinance, "download", lambda tickers, **kw: calls.append(tickers) or df)
    monkeypatch.setattr(prices, "CACHE", tmp_path)
    monkeypatch.setattr(prices, "STATE", tmp_path / "_state.json")
    monkeypatch.delenv("MARKETSTACK_API_KEY", raising=False)
    monkeypatch.delenv("PRICE_SOURCE", raising=False)
    px = prices.load(None, ["BRK.B"], date(2026, 1, 1), today=date(2026, 6, 2))
    assert calls == [["BRK-B", "SPY"]]  # Yahoo spelling requested
    assert px.close_on("BRK.B", date(2026, 6, 2)) == 410.0 and "yfinance" in px.source


# ---------- fixes after the first live run ----------

def make_old_ptr_pdf():
    """2022-style PTR: labels spelled out in odd case, some capitals printed lower case."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont("Helvetica", 7)
    t = lambda x, y, s: c.drawString(x, y, s)
    t(40, 740, "Periodic Transaction Report")
    xs = dict(id=40, owner=60, asset=100, type=300, date=360, notif=420, amount=480, gains=560)
    for k, s in [("id", "ID"), ("owner", "Owner"), ("asset", "Asset"), ("type", "Transaction"),
                 ("date", "Date"), ("notif", "Notification"), ("amount", "Amount"), ("gains", "Cap.")]:
        t(xs[k], 680, s)
    t(xs["type"], 672, "Type"); t(xs["notif"], 672, "Date"); t(xs["gains"], 672, "Gains >")
    t(xs["gains"], 664, "$200?")
    t(xs["owner"], 640, "SP"); t(xs["asset"], 640, "Alphabet Inc. - Class A (gOOgl) [ST]")
    t(xs["type"], 640, "P"); t(xs["date"], 640, "02/10/2022"); t(xs["notif"], 640, "02/15/2022")
    t(xs["amount"], 640, "$15,001 - $50,000")
    t(xs["asset"], 630, "FIlINg STATuS: New")
    t(xs["asset"], 620, "DESCRIPTION: Part of my spouse's retirement")
    t(xs["type"], 610, "Portfolio.")  # wrapped text that lands in the type column
    t(xs["asset"], 590, "lockheed Martin Corporation (lMT) [ST]"); t(xs["type"], 590, "S")
    t(xs["date"], 590, "02/11/2022"); t(xs["notif"], 590, "02/15/2022"); t(xs["amount"], 590, "$1,001 - $15,000")
    t(xs["asset"], 580, "FILINg STATUS: New"); t(xs["asset"], 570, "SubHOLdINg OF: Brokerage")
    t(xs["asset"], 560, "COMMENTS: Sold all")
    t(xs["asset"], 540, "US Treasury Bill [gS]"); t(xs["type"], 540, "P")
    t(xs["date"], 540, "02/12/2022"); t(xs["notif"], 540, "02/15/2022"); t(xs["amount"], 540, "$1,001 - $15,000")
    t(40, 520, "* For the complete list of asset type abbreviations, please visit https://fd.house.gov/reference/asset-type-codes.aspx.")
    c.save()
    return buf.getvalue()


def test_house_pdf_parser_old_style_labels():
    rows, expected = house.parse_pdf(make_old_ptr_pdf())
    assert expected == 3 and len(rows) == 3
    g, l, b = rows
    assert (g["asset_name"], g["ticker"], g["tx_type"]) == ("Alphabet Inc. - Class A", "GOOGL", "Purchase")
    assert g["comment"] == "D: Part of my spouse's retirement Portfolio."
    assert (l["ticker"], l["tx_type"], l["comment"]) == ("LMT", "Sale", "SO: Brokerage; C: Sold all")
    assert b["asset_type"] == "Government Security" and b["ticker"] == ""
    # Crypto names in brackets are not tickers
    assert not house.TICKER_RE.search("XRP (Ripple) [CT]")


def test_member_matching_former_members_and_switchers(members):
    assert members.match("House", "Manning", "Kathy", "NC", "06")["bioguide"] == "M001135"   # left 2025
    assert members.match("Senate", "Thomas R Carper", "Thomas R Carper")["bioguide"] == "C000174"
    assert members.match("House", "Mullin", "Markwayne", "OK", "02")["bioguide"] == "M001190"  # House, then Senate
    assert members.match("Senate", "Mullin", "Markwayne")["bioguide"] == "M001190"
    assert members.match("House", "Curtis", "John", "UT", "03")["bioguide"] == "C001114"     # now a senator
    assert "M001135" not in {r["bioguide"] for r in members.rows()}  # members page: current only


def test_llm_uses_auto_tool_choice_and_fails_cleanly():
    calls = {}
    ok = types.SimpleNamespace(type="tool_use", input={"transactions": [], "confidence": 1})
    before = llm.USAGE["out"]
    llm.extract_pdf(b"%PDF", client=fake_client([types.SimpleNamespace(type="text", text="note"), ok], calls=calls))
    assert calls["tool_choice"] == {"type": "auto"} and calls["thinking"] == {"type": "between_tools"}
    assert calls["max_tokens"] == 64000 and llm.USAGE["out"] == before + 500
    with pytest.raises(llm.AIError) as e:
        llm.extract_pdf(b"%PDF", client=fake_client([types.SimpleNamespace(type="text", text="Sorry")]))
    assert e.value.kind == "filing"
    with pytest.raises(llm.AIError) as e:
        llm.extract_pdf(b"%PDF", client=fake_client([ok], stop="max_tokens"))
    assert e.value.kind == "filing"


@pytest.mark.parametrize("status,text,kind", [
    (503, "credential validation failed", "transient"), (500, "Unable to complete", "transient"),
    (529, "Overloaded", "transient"), (429, "rate limit", "transient"),
    (401, "invalid x-api-key", "fatal"), (400, "Your credit balance is too low", "fatal"),
    (404, "model not found", "fatal"), (400, "Could not process PDF", "filing"),
])
def test_ai_error_kinds(status, text, kind):
    class Err(Exception):
        status_code = status
    assert llm.error_kind(Err(text)) == kind


def e2e_setup(tmp_path, monkeypatch, members, sectors, years=None):
    import pipeline
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.write(FIXTURES / "house_index_sample.xml", "2026FD.xml")
    pdf = make_ptr_pdf()
    sectors.cache_path = tmp_path / "sic.csv"
    monkeypatch.setattr(pipeline, "DATA", tmp_path)
    monkeypatch.setattr(pipeline, "session", lambda: None)
    monkeypatch.setattr(pipeline.Members, "load", classmethod(lambda cls, s=None: members))
    monkeypatch.setattr(pipeline, "Sectors", lambda s: sectors)

    def fetch_index(s, y):
        if years is not None:
            years.append(y)
        return house.parse_index(buf.getvalue()) if y == 2026 else []
    monkeypatch.setattr(pipeline.house, "fetch_index", fetch_index)
    monkeypatch.setattr(pipeline, "get", lambda s, url: types.SimpleNamespace(content=pdf))
    monkeypatch.setattr(pipeline.llm, "available", lambda: False)
    monkeypatch.setattr(pipeline.prices, "load", lambda *a, **k: None)
    return pipeline


def test_stale_parse_is_reread_and_former_members_rematched(tmp_path, monkeypatch, members, sectors):
    import csv
    years = []
    pipeline = e2e_setup(tmp_path, monkeypatch, members, sectors, years)
    old = {"chamber": "House", "filing_id": "20034916", "filing_date": "2026-07-10", "status": "parsed",
           "kind": "efile", "parser": ""}
    older = {"chamber": "House", "filing_id": "20011111", "filing_date": "2024-05-01", "status": "error",
             "kind": "efile", "attempts": "1"}
    common.write_csv(tmp_path / "filings.csv", [old, older], common.FILING_FIELDS)
    stale = [{"trade_id": f"H-20034916-{i}", "chamber": "House", "filing_id": "20034916", "filing_date": "2026-07-10",
              "asset_name": "Medtronic FIlINg STATuS: New", "status": "ok"} for i in (1, 2, 3)]
    carper = {"trade_id": "S-abc-1", "chamber": "Senate", "filing_id": "abc", "filing_date": "2024-03-01",
              "filer_name": "Thomas R Carper", "bioguide": "", "status": "ok", "ticker": "AAPL", "asset_type": "Stock"}
    common.write_csv(tmp_path / "trades.csv", stale + [carper], common.TRADE_FIELDS)

    assert pipeline.run(date(2026, 1, 1), ["house"]) == 0
    assert years[0] == 2024  # looked back to the older filing that still needs work
    trades = {t["trade_id"]: t for t in csv.DictReader(open(tmp_path / "trades.csv"))}
    assert "H-20034916-3" not in trades and "FIlINg" not in trades["H-20034916-1"]["asset_name"]
    assert trades["S-abc-1"]["bioguide"] == "C000174"
    filings = {r["filing_id"]: r for r in csv.DictReader(open(tmp_path / "filings.csv"))}
    assert filings["20034916"]["parser"] == f"text-{house.PARSER_VERSION}"


def test_errors_retry_limit_and_strict_threshold(tmp_path, monkeypatch, members, sectors):
    import csv
    pipeline = e2e_setup(tmp_path, monkeypatch, members, sectors)
    real = pipeline.parse_house

    def flaky(s, f, use_llm):
        if f["filing_id"] == "20034916":
            raise RuntimeError("403 Forbidden")
        return real(s, f, use_llm)
    monkeypatch.setattr(pipeline, "parse_house", flaky)
    status = lambda: {r["filing_id"]: r for r in csv.DictReader(open(tmp_path / "filings.csv"))}["20034916"]
    for n in (1, 2, 3):
        assert pipeline.run(date(2026, 1, 1), ["house"], strict=True) == 0  # 1 error: not "too many"
        assert status()["status"] == "error" and status()["attempts"] == str(n)
    pipeline.run(date(2026, 1, 1), ["house"])
    assert status()["attempts"] == "3"  # scheduled runs give up after 3 tries
    pipeline.run(date(2026, 1, 1), ["house"], retry_errors=True)
    assert status()["attempts"] == "4"  # a manual run tries again


def test_ai_switched_off_on_fatal_error(tmp_path, monkeypatch, members, sectors):
    import csv
    pipeline = e2e_setup(tmp_path, monkeypatch, members, sectors)
    monkeypatch.setattr(pipeline.llm, "available", lambda: True)

    def no_credit(pdf):
        raise llm.AIError("credit balance is too low", "fatal")
    monkeypatch.setattr(pipeline.llm, "extract_pdf", no_credit)
    assert pipeline.run(date(2026, 1, 1), ["house"], strict=True) == 1  # failed: you get an email
    filings = {r["filing_id"]: r for r in csv.DictReader(open(tmp_path / "filings.csv"))}
    assert filings["9116142"]["status"] == "needs_ai" and filings["20034916"]["status"] == "parsed"


def test_ai_paused_on_api_hiccups(tmp_path, monkeypatch, members, sectors):
    import csv
    pipeline = e2e_setup(tmp_path, monkeypatch, members, sectors)
    monkeypatch.setattr(pipeline.llm, "available", lambda: True)
    monkeypatch.setattr(pipeline, "AI_BREAKER", 1)
    calls = []

    def outage(pdf):
        calls.append(1)
        raise llm.AIError("503 credential validation failed", "transient")
    monkeypatch.setattr(pipeline.llm, "extract_pdf", outage)
    assert pipeline.run(date(2026, 1, 1), ["house"], strict=True) == 0  # a hiccup is not a failure
    filings = {r["filing_id"]: r for r in csv.DictReader(open(tmp_path / "filings.csv"))}
    assert filings["9116142"]["status"] == "needs_ai" and filings["9116142"]["attempts"] in ("", "0")
    assert len(calls) == 1


def test_senate_fetch_renews_session_on_403(monkeypatch):
    import requests

    def resp(code, text=""):
        r = requests.Response()
        r.status_code, r._content, r.url = code, text.encode(), "https://efdsearch.senate.gov/search/view/ptr/x/"
        return r
    answers = [resp(403)] * 3 + [resp(200, "<table></table>")]  # get() itself tries 3 times
    s = types.SimpleNamespace(get=lambda url, **kw: answers.pop(0))
    renewed = []
    monkeypatch.setattr(senate, "open_session", lambda s: renewed.append(1))
    monkeypatch.setattr(senate.time, "sleep", lambda n: None)
    monkeypatch.setattr(common.time, "sleep", lambda n: None)
    assert senate.fetch_ptr(s, "https://efdsearch.senate.gov/search/view/ptr/x/") == "<table></table>"
    assert renewed == [1]
