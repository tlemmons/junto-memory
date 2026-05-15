"""Unit tests for `with_op_log` — the §4.3.b transactional emission path.

Covers `design:local-first-junto-v0-mvp` v0.4.0 §4.3.b: Mongo-only
context manager that wraps the caller's write + op_log append in a
single replica-set transaction.

Contract under test:
- Clean exit commits the transaction; append rows land with the
  expected shape (seq monotonic, schema_version=1, origin/ts stamped).
- Exception inside the `with` block aborts the transaction and
  re-raises. No commit signal.
- Invalid op_type from `append` raises OpLogError (which then aborts
  the surrounding transaction).
- The yielded session is passed to next_seq and to the op_log
  insert_one, so a real Mongo would join them in the transaction.
- db=None raises OpLogError immediately.
- intent_id passed to append lands on the op_log row; default is None.
"""

import pytest

from shared_memory import op_log


class _FakeTransaction:
    """Tracks commit/abort signals from the session.start_transaction CM."""

    def __init__(self, session):
        self.session = session

    def __enter__(self):
        self.session.in_transaction = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.session.committed = True
        else:
            self.session.aborted = True
        self.session.in_transaction = False
        return False  # don't suppress the exception


class _FakeMongoSession:
    """A Mongo session: tracks tx lifecycle + is identity-compared for session=."""

    def __init__(self):
        self.in_transaction = False
        self.committed = False
        self.aborted = False
        self.ended = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.ended = True
        return False

    def start_transaction(self):
        return _FakeTransaction(self)


class _FakeMongoClient:
    def __init__(self):
        self.session = _FakeMongoSession()

    def start_session(self):
        return self.session


class _FakeOpLogCollection:
    """Records inserts with the session kwarg so tests can verify joining."""

    def __init__(self):
        self.inserts: list[tuple[dict, object]] = []  # (doc, session)

    def insert_one(self, doc, session=None):
        self.inserts.append((doc, session))

    def create_index(self, *args, **kwargs):
        pass


class _FakeMetaCollection:
    """Records session= on find_one_and_update so tests can verify."""

    def __init__(self):
        self._meta: dict[str, int] = {}
        self.sessions_seen: list[object] = []

    def find_one_and_update(self, filt, update, upsert=False, return_document=None, session=None):
        self.sessions_seen.append(session)
        origin = filt["_id"]
        delta = update.get("$inc", {}).get("seq", 0)
        if origin not in self._meta:
            if not upsert:
                return None
            self._meta[origin] = 0
        self._meta[origin] += delta
        return {"_id": origin, "seq": self._meta[origin]}

    def create_index(self, *args, **kwargs):
        pass


class _FakeMessagesCollection:
    """Mongo-backed caller write target — records session=."""

    def __init__(self):
        self.inserts: list[tuple[dict, object]] = []

    def insert_one(self, doc, session=None):
        self.inserts.append((doc, session))


class _FakeDB:
    def __init__(self):
        self.client = _FakeMongoClient()
        self._cols = {
            op_log.OPLOG_COLLECTION: _FakeOpLogCollection(),
            op_log.OPLOG_META_COLLECTION: _FakeMetaCollection(),
            "messages": _FakeMessagesCollection(),
        }

    def __getitem__(self, name):
        return self._cols[name]

    @property
    def messages(self):
        return self._cols["messages"]


def _actor():
    return {"agent": "memory", "project": "junto", "session_id": "test_session"}


def _ref():
    return {"collection": "messages", "doc_id": "msg_abc123"}


def test_with_op_log_commits_on_clean_exit():
    db = _FakeDB()
    with op_log.with_op_log(db) as (session, append):
        db.messages.insert_one({"_id": "msg_abc123", "body": "hi"}, session=session)
        entry = append("message.sent", _actor(), _ref(), {"body": "hi"})

    assert db.client.session.committed is True
    assert db.client.session.aborted is False
    assert db.client.session.ended is True
    assert entry["op_type"] == "message.sent"
    assert entry["seq"] == 1
    assert entry["schema_version"] == op_log.OPLOG_SCHEMA_VERSION
    assert len(db[op_log.OPLOG_COLLECTION].inserts) == 1


def test_with_op_log_aborts_on_caller_exception():
    db = _FakeDB()

    class BoomError(RuntimeError):
        pass

    with pytest.raises(BoomError):
        with op_log.with_op_log(db) as (session, append):
            db.messages.insert_one({"_id": "msg_x", "body": "hi"}, session=session)
            append("message.sent", _actor(), _ref(), {"body": "hi"})
            raise BoomError("simulated caller failure mid-block")

    assert db.client.session.committed is False
    assert db.client.session.aborted is True
    assert db.client.session.ended is True
    # The fake collections don't simulate transaction rollback (real Mongo
    # would discard the inserts on abort), but the abort signal is what we
    # assert on for the unit-test layer.


def test_with_op_log_passes_session_to_writes():
    db = _FakeDB()
    with op_log.with_op_log(db) as (session, append):
        db.messages.insert_one({"_id": "msg_y"}, session=session)
        append("message.sent", _actor(), _ref(), {})

    # The caller's message insert should have received the same session
    # the wrapper opened.
    msg_doc, msg_session = db.messages.inserts[0]
    assert msg_session is db.client.session

    # The op_log insert and the seq increment should also have used it.
    op_doc, op_session = db[op_log.OPLOG_COLLECTION].inserts[0]
    assert op_session is db.client.session
    meta = db[op_log.OPLOG_META_COLLECTION]
    assert meta.sessions_seen == [db.client.session]


def test_with_op_log_append_rejects_invalid_op_type():
    db = _FakeDB()

    with pytest.raises(op_log.OpLogError, match="invalid op_type"):
        with op_log.with_op_log(db) as (session, append):
            append("not.a.real.op", _actor(), _ref(), {})

    # OpLogError from append propagates through the transaction CM, which
    # aborts; nothing should have committed.
    assert db.client.session.committed is False
    assert db.client.session.aborted is True


def test_with_op_log_rejects_none_db():
    with pytest.raises(op_log.OpLogError, match="live Mongo db handle"):
        with op_log.with_op_log(None):
            pass


def test_with_op_log_multiple_appends_in_one_transaction():
    """A single tx can record more than one op_log row (e.g., a tool that
    writes two related Mongo docs and wants both audited atomically)."""
    db = _FakeDB()
    with op_log.with_op_log(db) as (session, append):
        e1 = append("message.sent", _actor(), _ref(), {"i": 1})
        e2 = append("message.sent", _actor(), _ref(), {"i": 2})

    assert e1["seq"] == 1
    assert e2["seq"] == 2
    assert db.client.session.committed is True
    assert len(db[op_log.OPLOG_COLLECTION].inserts) == 2


def test_with_op_log_append_default_intent_id_is_none():
    db = _FakeDB()
    with op_log.with_op_log(db) as (session, append):
        entry = append("message.sent", _actor(), _ref(), {})

    assert entry["intent_id"] is None


def test_with_op_log_append_threads_explicit_intent_id():
    db = _FakeDB()
    with op_log.with_op_log(db) as (session, append):
        entry = append(
            "message.sent",
            _actor(),
            _ref(),
            {},
            intent_id="intent-from-caller-42",
        )

    assert entry["intent_id"] == "intent-from-caller-42"


def test_with_op_log_entry_shape():
    """Same shape contract as §4.3.a emit_op_log: schema_version, origin,
    ts, op_type, actor, ref, payload, seq, _id."""
    db = _FakeDB()
    with op_log.with_op_log(db) as (session, append):
        entry = append("message.sent", _actor(), _ref(), {"body": "hello"})

    assert entry["_id"].startswith("op_")
    assert entry["origin"]  # stamped from ORIGIN_SERVER_ID
    assert "T" in entry["ts"]  # ISO-8601
    assert entry["actor"] == _actor()
    assert entry["ref"] == _ref()
    assert entry["payload"] == {"body": "hello"}
    assert entry["schema_version"] == op_log.OPLOG_SCHEMA_VERSION


def test_with_op_log_caller_exception_propagates_unmodified():
    """The exception type and message survive the abort path — callers
    must be able to catch their own errors after the transaction unwinds."""
    db = _FakeDB()

    class SpecificError(ValueError):
        pass

    try:
        with op_log.with_op_log(db) as (session, append):
            append("message.sent", _actor(), _ref(), {})
            raise SpecificError("the original message")
    except SpecificError as exc:
        assert str(exc) == "the original message"
    else:  # pragma: no cover
        pytest.fail("SpecificError was swallowed")
