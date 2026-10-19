# stock-screener-data

Public, data-only half of a personal stock screener.

- `edgar_fetch.py` pulls financial-statement facts from SEC EDGAR's free companyfacts API for the
  tickers in `universe.txt` and writes `edgar_data/<date>.json` and `edgar_data/latest.json`.
- A GitHub Action runs it every Sunday at 22:00 UTC (and whenever the fetcher or ticker list changes).
- Everything here is public SEC data or a plain fetcher. The screening rules and scoring code live
  in a separate private repository.

The SEC requires a contact in the User-Agent; it is read from the `SEC_USER_AGENT` repository
secret and is never committed.

`code/screener-code.tar.enc` is an AES-256 encrypted bundle of the private screening code, so a
scheduled run can load it byte-for-byte without access to the private repo. The key is not here.
