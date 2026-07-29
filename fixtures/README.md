# Captured API fixtures

Drop real API responses here so the parsers can be validated against genuine
payloads rather than assumptions.

```bash
python -m btcbot verify-venue --dump fixtures/kalshi.json
```

Run that from a machine that can reach the venue, then commit the file.
`tests/test_fixtures.py` picks it up automatically and skips until it exists.

**Contents are public market data only** — open markets and their orderbooks.
No credentials, no balances, no positions. Read the file before committing it
anyway.
