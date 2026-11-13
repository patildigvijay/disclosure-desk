"""Add member, committee, sector and overlap info to parsed trades.

Committee seats: github.com/unitedstates/congress-legislators (public domain, updated
as assignments change). Sectors: SEC SIC code of each listed company, mapped to a
sector in config/sic_sectors.csv. Committee -> sector mapping: config/committee_sectors.csv.
"""
import csv
import json
import re
import time
import unicodedata
from datetime import date

import yaml

from common import CONFIG, DATA, get, read_csv, write_csv

GH = "https://raw.githubusercontent.com/unitedstates/congress-legislators/main"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
CACHE = DATA / "cache"
EQUITY_WORDS = ("stock", "option")


def norm_name(text):
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"\b(hon|jr|sr|ii|iii|iv|mr|mrs|ms|dr)\b\.?", " ", t)
    return re.sub(r"[^a-z ]", " ", t).split()


# ---------- members and committees ----------

class Members:
    def __init__(self, legislators, membership, committees=()):
        today = date.today().isoformat()
        self.people = []
        for p in legislators:
            term = p["terms"][-1]
            n = p["name"]
            self.people.append({
                "bioguide": p["id"]["bioguide"],
                "name": n.get("official_full") or f"{n.get('first', '')} {n.get('last', '')}".strip(),
                "type": term["type"],
                "state": term["state"],
                "district": term.get("district"),
                "party": term.get("party", ""),
                "leadership": [r["title"] for r in p.get("leadership_roles", [])
                               if not r.get("end") or str(r["end"]) >= today],
                "last": norm_name(n.get("last")),
                "firsts": set(norm_name(" ".join(filter(None, [n.get("first"), n.get("nickname"),
                                                              n.get("middle")])))),
            })
        self.committee_names = {c["thomas_id"]: c["name"] for c in committees}
        self.committees, self.titles = {}, {}
        for cid, members in membership.items():
            for m in members:
                if m.get("bioguide"):
                    self.committees.setdefault(m["bioguide"], []).append(cid)
                    if len(cid) == 4 and m.get("title") in ("Chair", "Chairman", "Ranking Member"):
                        name = self.committee_names.get(cid, cid)
                        self.titles.setdefault(m["bioguide"], []).append(f"{m['title']}, {name}")

    def rows(self):
        """One row per current member, for data/members.csv (used by the website)."""
        for p in self.people:
            cids = sorted(c for c in self.committees.get(p["bioguide"], []) if len(c) == 4)
            yield {
                "bioguide": p["bioguide"], "name": p["name"],
                "chamber": "House" if p["type"] == "rep" else "Senate",
                "party": p["party"], "state": p["state"],
                "district": "" if p["district"] is None else p["district"],
                "leadership": "; ".join(p["leadership"] + self.titles.get(p["bioguide"], [])),
                "committees": "; ".join(self.committee_names.get(c, c) for c in cids),
            }

    @classmethod
    def load(cls, s=None, folder=None):
        """Load from a local folder (tests) or download (and cache) from GitHub."""
        def read(name):
            if folder:
                return yaml.safe_load(open(folder / name, encoding="utf-8"))
            CACHE.mkdir(parents=True, exist_ok=True)
            text = get(s, f"{GH}/{name}").text
            (CACHE / name).write_text(text, encoding="utf-8")
            return yaml.safe_load(text)
        return cls(read("legislators-current.yaml"), read("committee-membership-current.yaml"),
                   read("committees-current.yaml"))

    def match(self, chamber, last_name, first_name="", state="", district=""):
        """Return the matching current member dict, or None."""
        kind = "rep" if chamber == "House" else "sen"
        last = norm_name(last_name)
        firsts = set(norm_name(first_name))
        pool = [p for p in self.people if p["type"] == kind]

        def last_ok(p):
            return p["last"] == last or (p["last"] and p["last"][-1:] == last[-1:])

        if kind == "rep" and state:
            dist = int(district) if str(district).isdigit() else None
            seat = [p for p in pool if p["state"] == state and p["district"] == dist and last_ok(p)]
            if len(seat) == 1:
                return seat[0]
        cands = [p for p in pool if last_ok(p) and (not state or p["state"] == state)]
        if len(cands) > 1 and firsts:
            cands = [p for p in cands if p["firsts"] & firsts] or cands
        return cands[0] if len(cands) == 1 else None


def load_committee_sectors():
    out = {}
    with open(CONFIG / "committee_sectors.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[r["committee_id"]] = {"name": r["committee_name"],
                                      "sectors": set(filter(None, r["sectors"].split("|")))}
    return out


# ---------- tickers and sectors ----------

class Sectors:
    def __init__(self, s=None, tickers=None, sic_cache=None):
        self.s = s
        self.ranges = [(int(r["sic_from"]), int(r["sic_to"]), r["sector"])
                       for r in read_csv(CONFIG / "sic_sectors.csv")]
        self.tickers = tickers  # ticker -> cik
        self.cache_path = CACHE / "sic.csv"
        self.sic = sic_cache if sic_cache is not None else {
            r["ticker"]: r for r in read_csv(self.cache_path)}

    def load_tickers(self):
        if self.tickers is None:
            CACHE.mkdir(parents=True, exist_ok=True)
            data = get(self.s, SEC_TICKERS).json()
            (CACHE / "sec_tickers.json").write_text(json.dumps(data), encoding="utf-8")
            self.tickers = {v["ticker"].upper(): int(v["cik_str"]) for v in data.values()}
        return self.tickers

    @staticmethod
    def sec_form(ticker):
        return ticker.upper().replace(".", "-").replace("/", "-")

    def known(self, ticker):
        return self.sec_form(ticker) in self.load_tickers()

    def sector_for_sic(self, sic):
        try:
            sic = int(sic)
        except (TypeError, ValueError):
            return ""
        for lo, hi, sector in self.ranges:
            if lo <= sic <= hi:
                return sector
        return ""

    def lookup(self, ticker):
        """ticker -> (sic, sector). Cached; one SEC call per new ticker."""
        t = self.sec_form(ticker)
        if not t:
            return "", ""
        if t not in self.sic:
            cik = self.load_tickers().get(t)
            row = {"ticker": t, "cik": cik or "", "sic": "", "sic_desc": ""}
            if cik:
                time.sleep(0.15)  # SEC fair-access limit is 10 requests/second
                sub = get(self.s, SEC_SUBMISSIONS.format(cik=cik)).json()
                row.update(sic=sub.get("sic", ""), sic_desc=sub.get("sicDescription", ""))
            self.sic[t] = row
        sic = self.sic[t]["sic"]
        return sic, self.sector_for_sic(sic)

    def save(self):
        write_csv(self.cache_path, self.sic.values(), ["ticker", "cik", "sic", "sic_desc"])


def is_equity(asset_type):
    a = (asset_type or "").lower()
    return any(w in a for w in EQUITY_WORDS) and "non-public" not in a and "private" not in a


# ---------- combine ----------

def enrich(trade, filing, members, committee_sectors, sectors):
    """Fill member, sector and overlap fields on one trade (in place)."""
    m = members.match(filing["chamber"], filing.get("last_name", ""), filing.get("first_name", ""),
                      filing.get("state", ""), filing.get("district", ""))
    cids = members.committees.get(m["bioguide"], []) if m else []
    trade.update(bioguide=m["bioguide"] if m else "", party=m["party"] if m else "",
                 state=filing.get("state") or (m["state"] if m else ""),
                 member_committees="|".join(sorted(c for c in cids if len(c) == 4)))
    sector = ""
    if trade.get("ticker") and is_equity(trade.get("asset_type")):
        _, sector = sectors.lookup(trade["ticker"])
    hits = [committee_sectors[c]["name"] for c in sorted(cids)
            if c in committee_sectors and sector in committee_sectors[c]["sectors"]]
    trade.update(sector=sector, overlap=bool(hits), overlap_committees="; ".join(hits))
    return trade
