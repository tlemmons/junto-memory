"""Write-time secret redaction (Memento #3, interface:secret-redaction-v0).

POSITIVES must lose the secret and keep the surrounding text. NEGATIVES are
shapes the 2026-10-30 corpus dry-run (34,108 docs) showed agents write all the
time: env-var NAMES, code references, placeholders, prose about passwords.
Over-redaction there would eat real knowledge, so each one is pinned.
"""

import pytest

from shared_memory.redact import redact_fields, redact_secrets, redaction_notice

# (text, secret substring that must disappear, expected kind)
# Every value below is a FAKE fixture credential made up for these tests (none is
# copied or shape-preserved from a real one). `# nosemgrep` marks each line so
# secret scanners read them as fixtures; keep new fixtures obviously fake.
POSITIVES = [
    ("key sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "sk-ant-api03-AbCd", "anthropic_key"),  # nosemgrep
    ("export OPENAI_API_KEY=sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "sk-proj-AbCd", "openai_key"),  # nosemgrep
    ("JUNTO_API_KEY=smk_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-x", "smk_AbCd", "junto_key"),  # nosemgrep
    ("aws AKIAIOSFODNN7EXAMPLE here", "AKIAIOSFODNN7EXAMPLE", "aws_access_key"),  # nosemgrep
    ("gh token ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "ghp_AbCd", "github_token"),  # nosemgrep
    ("slack xoxb-1234567890-abcdefghij", "xoxb-1234567890", "slack_token"),  # nosemgrep
    ("maps AIzaSyA-1234567890abcdefghijklmnopqrstu", "AIzaSyA-1234", "google_api_key"),  # nosemgrep
    ("stripe sk_live_AbCdEfGhIjKlMnOpQrStUv", "sk_live_AbCd", "stripe_key"),  # nosemgrep
    ("HA_TOKEN=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",  # nosemgrep
     "eyJhbGciOiJIUzI1NiJ9", "jwt"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAA\n-----END OPENSSH PRIVATE KEY-----",  # nosemgrep
     "b3BlbnNzaC1rZXktdjEAAAA", "private_key"),
    ("MONGO_URI='mongodb://admin:FakePw11@localhost:27017'", "FakePw11", "url_password"),  # nosemgrep
    ('curl -H "Authorization: Bearer 46d8f0a1b2c3d4e5f6a7b8c9d0e1f2a3"', "46d8f0a1b2c3", "auth_header"),  # nosemgrep
    ("MONGO_DEV_PASSWORD=FakeTst99", "FakeTst99", "assigned_secret"),  # nosemgrep
    ('"mqttPassword": "fake8f9c-77d2-4b1e-9c0d-5e6f7a8b9c0d"', "fake8f9c", "assigned_secret"),  # nosemgrep
    ("Server=db.example.invalid;Port=3306;User=app;Password=FAKEx9q2LmZ7vT3", "FAKEx9q2LmZ7vT3", "assigned_secret"),  # nosemgrep
    ("Clarity API - Token: secXk29dLq8vNmZ3pR7tWb4yHc6jFg1s", "secXk29dLq8v", "assigned_secret"),  # nosemgrep
    ("cloudflared tunnel run --token eyJhIjoiMjlmMTRmNTg0ODlmN2Y0ODBlODRhZDA2MzUwNTJmZTMiLCJ0IjoiMGNiNmMz",  # nosemgrep
     "eyJhIjoiMjlm", "encoded_token"),
    ("mysql --password=Qz8vLm2kRt7x -h db", "Qz8vLm2kRt7x", "cli_secret"),  # nosemgrep
    ("tool --api-key a8F3kLq9Zx2Vb7Nm", "a8F3kLq9Zx2Vb7Nm", "cli_secret"),  # nosemgrep
    ("DB_EXAMPLE_STAGING_PASS=FAKEvXk2LpR9tWz4NbH6jYc3D", "FAKEvXk2LpR9tWz4NbH6jYc3D", "assigned_secret"),  # nosemgrep
    ("- example_ro @ db.example.invalid:3306 : FAKEzPw8NxL2vKt6RmB9sYd3F", "FAKEzPw8NxL2vKt6RmB9sYd3F", "conn_secret"),  # nosemgrep
    ("user: app_rw pass: 4f9a2c7e1b8d3f6a9c2e5b8d1f4a7c0e", "4f9a2c7e1b8d3f6a9c2e5b8d", "assigned_secret"),  # nosemgrep
]

NEGATIVES = [
    "the password is in 1Password",
    "set MONGO_PASSWORD in .env, never commit it",
    "password=os.environ['MONGO_PASSWORD']",
    "api_key: settings.API_KEY",
    "_apiKey = ConfigurationManager.AppSettings[\"StripeKey\"]",
    "GoogleMapsPlacesApiKey: SystemConfig",
    "token: analyst/secrets/ga4-sa-key.json",
    "token = <your-token-here>",
    "password=${MONGO_PASSWORD}",
    "f\"mongodb://{user}:{password}@host\"",
    "rtsp://admin:PW@camera",
    "secret = MEDIA_RENDITION_SHARED_SECRET",
    "see token_count=12345678 in the log",
    "max_tokens=8192",
    "token = 2026-10-30",
    "password: changeme",
    "Authorization: Bearer $TOKEN",
    "already [REDACTED:assigned_secret] earlier",
    "pass --token $CF_TUNNEL_TOKEN from the env",
    "the --password flag is required",
    'header eyJhbGciOiJIUzI1NiJ9 alone is not a token',
    "memory@junto: msg_abc123def456 answered",
    "db@host:27017: up",
    "bypass: true",
    "the pass = stale check",
    "Reviews PASS: f1c17da5 on staging",
    "grep PASS = sku=99.999|x9_ab",
]


@pytest.mark.parametrize("text,secret,kind", POSITIVES)
def test_positive_redacted(text, secret, kind):
    out, found = redact_secrets(text)
    assert secret not in out
    assert f"[REDACTED:{kind}]" in out
    assert found.get(kind, 0) >= 1


@pytest.mark.parametrize("text", NEGATIVES)
def test_negative_untouched(text):
    out, found = redact_secrets(text)
    assert out == text, found
    assert found == {}


def test_key_name_survives_value_replaced():
    out, _ = redact_secrets("MONGO_DEV_PASSWORD=FakeTst99 for the dev box")
    assert out == "MONGO_DEV_PASSWORD=[REDACTED:assigned_secret] for the dev box"


def test_url_keeps_user_and_host():
    out, _ = redact_secrets("mongodb://admin:FakePw11@localhost:27017/db")
    assert out == "mongodb://admin:[REDACTED:url_password]@localhost:27017/db"


def test_idempotent():
    once, _ = redact_secrets("MONGO_DEV_PASSWORD=FakeTst99")
    twice, found = redact_secrets(once)
    assert twice == once and found == {}


def test_none_and_empty_pass_through():
    assert redact_secrets(None) == (None, {})
    assert redact_secrets("") == ("", {})


def test_redact_fields_merges_counts_and_keeps_order():
    (a, b, c), found = redact_fields("password=Xy9#kq2Lm", None, "AKIAIOSFODNN7EXAMPLE")
    assert b is None
    assert "Xy9#kq2Lm" not in a and "AKIA" not in c
    assert found == {"assigned_secret": 1, "aws_access_key": 1}


def test_notice_shape():
    n = redaction_notice({"jwt": 2, "url_password": 1})
    assert n["redacted"] == {"jwt": 2, "url_password": 1}
    assert "3 secret(s)" in n["redacted_warning"]
    assert "NOT persisted" in n["redacted_warning"]


# --- redaction_events (metrics) -------------------------------------------------
def test_hits_carry_kind_key_len_never_the_value():
    hits = []
    redact_secrets("MONGO_DEV_PASSWORD=FakeTst99 and mongodb://admin:FakePw11@h", hits)  # nosemgrep
    assert {"kind": "assigned_secret", "key": "MONGO_DEV_PASSWORD", "len": 9} in hits
    assert {"kind": "url_password", "key": "mongodb", "len": 8} in hits
    assert "Fake" not in repr(hits)


def test_redact_fields_logs_one_event_with_field_names(monkeypatch):
    from shared_memory import redact as r
    from shared_memory.state import active_sessions
    logged = []

    class _Col:
        def create_index(self, *a, **k):
            pass

        def insert_one(self, doc):
            logged.append(doc)

    class _DB(dict):
        def __getitem__(self, k):
            return _Col()

    monkeypatch.setattr("shared_memory.clients.get_mongo", lambda: _DB())
    monkeypatch.setattr(r, "_events_indexed", False)
    col = _Col()
    monkeypatch.setattr(_DB, "__getitem__", lambda self, k: col)
    active_sessions["rs"] = {"project": "nimbus", "claude_instance": "billing-team"}
    try:
        redact_fields("ok", "token: secXk29dLq8vNmZ3pR7tWb4yHc6jFg1s",
                      tool="memory_store", session_id="rs", fields=["title", "content"])
        redact_fields("nothing here", tool="memory_store", session_id="rs", fields=["title"])
    finally:
        active_sessions.pop("rs", None)
    assert len(logged) == 1
    ev = logged[0]
    assert ev["tool"] == "memory_store" and ev["project"] == "nimbus" and ev["agent"] == "billing-team"
    assert ev["hits"] == [{"kind": "assigned_secret", "key": "token", "len": 32, "field": "content"}]


# backlog_c4c5a7dd19ce: prose false positives from the 2026-11-04 stored-data
# scan. Values are obviously fake.
def test_prose_pass_is_not_a_key():
    for text in ("## Handoff Notes Next pass: rerun_the_backfill2 tonight",  # nosemgrep
                 "Acceptance (f) and (c) pass: f1c17da5e9 and 7cc0b78f0e",  # nosemgrep
                 "efuse unreviewed commits. Bypass: admin-merge-v2 on main",  # nosemgrep
                 "Optional same-pass: fixup_rows_v3 after ACL lands"):  # nosemgrep
        assert redact_secrets(text) == (text, {})


def test_pass_with_credential_context_still_redacts():
    for text in ("user: bob pass: Fak3Pw-xyz-123",  # nosemgrep
                 "ssh pi@frame-host pass: Fak3Pw-xyz-123",  # nosemgrep
                 "DB_PASS: Fak3Pw-xyz-123", "db_pass: Fak3Pw-xyz-123"):  # nosemgrep
        out, found = redact_secrets(text)
        assert "Fak3Pw-xyz-123" not in out and found == {"assigned_secret": 1}, text


def test_time_is_not_a_conn_handle():
    text = "2 adjacent ~20s native holds @16:02: frameStateChangedXYZ987"
    assert redact_secrets(text) == (text, {})


def test_conn_handle_with_host_still_redacts():
    out, found = redact_secrets("staging_rw @ db.example.test:3306 : Fak3Pw-xyz-1234")  # nosemgrep
    assert "Fak3Pw-xyz-1234" not in out and found == {"conn_secret": 1}
