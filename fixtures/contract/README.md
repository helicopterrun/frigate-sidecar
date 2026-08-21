# Contract fixtures (sidecar <-> Elsinore/FrigateKit)

Golden JSON documents of the sidecar's wire contract with the iOS app:
Live Activity payloads and content-states (`push/live_activities.py`), the
card push body (`push/delivery.py`), and the `/v1/push/settings`,
`/v1/push/decisions`, and `/v1/capabilities` responses. Every file is built
from fixed, canned inputs by `tests/test_golden_contract_fixtures.py`, which
byte-compares them on every run — a contract change is a loud diff here, not
a phone that silently stops decoding.

`MANIFEST.json` holds a sha256 per fixture; the test verifies it, and the
app repo re-verifies its vendored copy independently.

Do not hand-edit. To re-bless after an intentional contract change:

    CONTRACT_GOLDEN_REGEN=1 pytest tests/test_golden_contract_fixtures.py

The app repo vendors these verbatim via
`Elsinore/tools/sync-contract-fixtures.sh` — run it (and `swift test` in
`Elsinore/FrigateKit`) after any regen here so both repos stay in lockstep.
