"""Facets and the claim cache are redacted at write (infra-team msg_eec26d1a9b4d)."""

from shared_memory import claim_gate, facets


class _Col:
    def __init__(self):
        self.rows = {}

    def update_one(self, flt, upd, upsert=False):
        self.rows.setdefault(flt["_id"], {}).update(upd["$set"])


class _DB(dict):
    def __missing__(self, name):
        self[name] = _Col()
        return self[name]


def test_store_facets_redacts_claim_and_triggers():
    db = _DB()
    facets._store_facets(db, "learning_x", {
        "claim": 'Grafana admin reset to a new password "Fak3Pw-xyz-12345".',  # nosemgrep
        "trigger": ["when logging in with password: Fak3Pw-xyz-12345", "when Grafana returns 401"],  # nosemgrep
        "operation": "config", "shelf_life": None,
    })
    row = db[facets.FACETS_COLLECTION].rows["learning_x"]
    assert "Fak3Pw-xyz-12345" not in row["claim"] and "[REDACTED:" in row["claim"]
    assert "Fak3Pw-xyz-12345" not in row["trigger"][0] and row["trigger"][1] == "when Grafana returns 401"
    assert "shelf_life" not in row


def test_cache_claim_redacts():
    db = _DB()
    claim_gate._cache_claim(db, "learning_x", 'The password is "Fak3Pw-xyz-12345".')  # nosemgrep
    assert "Fak3Pw-xyz-12345" not in db[claim_gate.CLAIMS_COLLECTION].rows["learning_x"]["claim"]
