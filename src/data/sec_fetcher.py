"""
SEC EDGAR Data Fetcher — Data Engineering Team
Pulls 13F-HR filings for tracked institutions.
Uses the public SEC EDGAR API (no key required).
"""

import requests
import json
import time
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import pandas as pd

logger = logging.getLogger(__name__)


class SECFetcher:
    """
    Fetches institutional holdings data from SEC EDGAR.

    13F-HR filings are quarterly mandatory disclosures for institutional
    investment managers with >$100M AUM. They show equity holdings as of
    the end of each quarter (filed within 45 days).

    Strategy insight: By comparing consecutive 13F filings, we can identify
    which institutions are QUIETLY ACCUMULATING specific stocks — the "small
    block" signature we're hunting.
    """

    BASE_URL = "https://data.sec.gov"
    SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
    COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    FILING_INDEX_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=13F-HR&dateb=&owner=include&count=40&search_text="

    def __init__(self, user_agent: str):
        """
        Args:
            user_agent: Required by SEC EDGAR (format: 'Name email@domain.com')
        """
        self.headers = {
            "User-Agent": user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Host": "data.sec.gov",
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self._cache: Dict[str, dict] = {}

    def _get(self, url: str, delay: float = 0.15) -> Optional[dict]:
        """Rate-limited GET with caching. SEC allows ~10 req/sec."""
        if url in self._cache:
            return self._cache[url]
        try:
            time.sleep(delay)
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            self._cache[url] = data
            return data
        except requests.RequestException as e:
            logger.error(f"SEC fetch failed for {url}: {e}")
            return None

    def get_institution_submissions(self, cik: str) -> Optional[dict]:
        """Fetch all filing submissions for an institution."""
        cik_padded = cik.zfill(10)
        url = f"{self.BASE_URL}/submissions/CIK{cik_padded}.json"
        return self._get(url)

    def get_13f_filings(self, cik: str, max_filings: int = 8) -> List[dict]:
        """
        Get the most recent 13F-HR filing metadata for an institution.

        Returns list of dicts with: {accessionNumber, filingDate, reportDate, ...}
        """
        submissions = self.get_institution_submissions(cik)
        if not submissions:
            return []

        filings = []
        recent = submissions.get("filings", {}).get("recent", {})

        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        report_dates = recent.get("reportDate", [])

        for i, form in enumerate(forms):
            if form in ("13F-HR", "13F-HR/A"):
                filings.append({
                    "form": form,
                    "filingDate": dates[i] if i < len(dates) else None,
                    "reportDate": report_dates[i] if i < len(report_dates) else None,
                    "accessionNumber": accessions[i] if i < len(accessions) else None,
                    "cik": cik,
                    "institution": submissions.get("name", "Unknown"),
                })
                if len(filings) >= max_filings:
                    break

        return filings

    def get_holdings_from_filing(self, cik: str, accession_number: str) -> pd.DataFrame:
        """
        Fetch the actual holdings table from a specific 13F filing.

        Returns DataFrame with columns:
            ticker, cusip, shares, value_usd, security_type, put_call
        """
        cik_padded = cik.zfill(10)
        acc_clean = accession_number.replace("-", "")
        index_url = (
            f"https://www.sec.gov/Archives/edgar/full-index/"
            f"{acc_clean[:4]}/{acc_clean[4:6]}/"
            f"{accession_number}-index.htm"
        )

        # Try the direct XML filing approach
        xml_url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{acc_clean}/primary-document.xml"
        )

        # For production, you would parse the actual XML/HTML filing.
        # The SEC EDGAR API provides this data in structured form for recent filings.
        # We use the company facts endpoint as a reliable alternative.
        return self._parse_holdings_via_api(cik, accession_number)

    def _parse_holdings_via_api(self, cik: str, accession_number: str) -> pd.DataFrame:
        """
        Fallback: retrieve holdings via the EDGAR viewer API.
        This endpoint returns structured JSON for 13F filings.
        """
        cik_int = str(int(cik))
        acc_clean = accession_number.replace("-", "")
        url = (
            f"https://efts.sec.gov/LATEST/search-index?q=%22{accession_number}%22"
            f"&dateRange=custom&startdt=2013-01-01&enddt=2025-12-31&forms=13F-HR"
        )
        # Return empty for now — the real parsing is in get_holdings_delta()
        return pd.DataFrame()

    def get_holdings_delta(
        self, cik: str, institution_name: str
    ) -> pd.DataFrame:
        """
        Core method: compare the two most recent 13F filings to identify
        NEW positions and INCREASED positions — our primary signal.

        Returns DataFrame with columns:
            ticker, institution, change_type, shares_prev, shares_curr,
            shares_delta, shares_delta_pct, report_date_curr, report_date_prev
        """
        filings = self.get_13f_filings(cik, max_filings=2)
        if len(filings) < 2:
            logger.warning(f"Insufficient 13F filings for {institution_name}")
            return pd.DataFrame()

        current_filing = filings[0]
        previous_filing = filings[1]

        curr_holdings = self._fetch_structured_holdings(
            cik, current_filing["accessionNumber"]
        )
        prev_holdings = self._fetch_structured_holdings(
            cik, previous_filing["accessionNumber"]
        )

        if curr_holdings.empty or prev_holdings.empty:
            return pd.DataFrame()

        # Merge on ticker/CUSIP to compute delta
        merged = curr_holdings.merge(
            prev_holdings[["cusip", "shares", "value_usd"]],
            on="cusip",
            how="outer",
            suffixes=("_curr", "_prev"),
        )

        merged["shares_curr"] = merged["shares_curr"].fillna(0)
        merged["shares_prev"] = merged["shares_prev"].fillna(0)
        merged["shares_delta"] = merged["shares_curr"] - merged["shares_prev"]
        merged["shares_delta_pct"] = (
            merged["shares_delta"] / merged["shares_prev"].replace(0, 1) * 100
        )

        # Flag change type
        def classify(row):
            if row["shares_prev"] == 0 and row["shares_curr"] > 0:
                return "NEW_POSITION"
            elif row["shares_curr"] == 0 and row["shares_prev"] > 0:
                return "CLOSED_POSITION"
            elif row["shares_delta"] > 0:
                return "INCREASED"
            elif row["shares_delta"] < 0:
                return "DECREASED"
            return "UNCHANGED"

        merged["change_type"] = merged.apply(classify, axis=1)
        merged["institution"] = institution_name
        merged["report_date_curr"] = current_filing.get("reportDate", "")
        merged["report_date_prev"] = previous_filing.get("reportDate", "")

        # Only return actionable signals
        actionable = merged[
            merged["change_type"].isin(["NEW_POSITION", "INCREASED"])
        ].copy()

        return actionable.sort_values("shares_delta_pct", ascending=False)

    def _fetch_structured_holdings(self, cik: str, accession_number: str) -> pd.DataFrame:
        """
        Fetch holdings from EDGAR's structured data endpoint.
        Returns DataFrame: cusip, ticker, company_name, shares, value_usd
        """
        if not accession_number:
            return pd.DataFrame()

        cik_int = int(cik)
        acc_clean = accession_number.replace("-", "")

        # Try the EDGAR structured viewer endpoint
        url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
            f"{acc_clean}/{accession_number}-index.json"
        )

        # Attempt to fetch filing index
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": self.headers["User-Agent"]},
                timeout=15,
            )
            if resp.ok:
                index_data = resp.json()
                # Find the primary XML document
                for item in index_data.get("directory", {}).get("item", []):
                    if item.get("name", "").endswith(".xml"):
                        xml_url = (
                            f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
                            f"{acc_clean}/{item['name']}"
                        )
                        return self._parse_13f_xml(xml_url)
        except Exception as e:
            logger.debug(f"Filing index fetch failed: {e}")

        return pd.DataFrame()

    def _parse_13f_xml(self, xml_url: str) -> pd.DataFrame:
        """Parse 13F-HR XML document into holdings DataFrame."""
        try:
            resp = requests.get(
                xml_url,
                headers={"User-Agent": self.headers["User-Agent"]},
                timeout=30,
            )
            if not resp.ok:
                return pd.DataFrame()

            # Parse XML holdings table
            import xml.etree.ElementTree as ET
            root = ET.fromstring(resp.content)

            # 13F XML namespace
            ns = {
                "": "http://www.sec.gov/edgar/document/thirteenf/informationtable",
                "ns1": "http://www.sec.gov/edgar/document/thirteenf/informationtable",
            }

            records = []
            for info_table in root.iter():
                if "infoTable" in info_table.tag:
                    record = {}
                    for child in info_table:
                        tag = child.tag.split("}")[-1]  # strip namespace
                        if tag == "cusip":
                            record["cusip"] = child.text
                        elif tag == "nameOfIssuer":
                            record["company_name"] = child.text
                        elif tag == "titleOfClass":
                            record["security_type"] = child.text
                        elif tag == "value":
                            record["value_usd"] = int(child.text or 0) * 1000
                        elif tag == "shrsOrPrnAmt":
                            for sub in child:
                                if "sshPrnamt" in sub.tag:
                                    record["shares"] = int(sub.text or 0)
                        elif tag == "putCall":
                            record["put_call"] = child.text
                    if record.get("cusip"):
                        records.append(record)

            if not records:
                return pd.DataFrame()

            df = pd.DataFrame(records)
            df["ticker"] = df.get("cusip", "")  # CUSIP → ticker mapping would need external DB
            return df

        except Exception as e:
            logger.debug(f"XML parse failed for {xml_url}: {e}")
            return pd.DataFrame()

    def get_all_institutions_signals(
        self, institutions: Dict[str, str]
    ) -> pd.DataFrame:
        """
        Aggregate signals from all tracked institutions.
        Returns combined DataFrame of all NEW/INCREASED positions.
        """
        all_signals = []
        for name, cik in institutions.items():
            logger.info(f"Fetching 13F delta for {name}...")
            delta = self.get_holdings_delta(cik, name)
            if not delta.empty:
                all_signals.append(delta)
            time.sleep(0.5)  # Respectful rate limiting

        if not all_signals:
            return pd.DataFrame()

        combined = pd.concat(all_signals, ignore_index=True)

        # Count how many institutions are buying the same stock
        if "cusip" in combined.columns:
            institution_count = (
                combined[combined["change_type"].isin(["NEW_POSITION", "INCREASED"])]
                .groupby("cusip")["institution"]
                .nunique()
                .reset_index()
                .rename(columns={"institution": "institution_count"})
            )
            combined = combined.merge(institution_count, on="cusip", how="left")

        return combined
