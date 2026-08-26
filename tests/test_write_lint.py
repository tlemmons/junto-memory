"""Write-side lint — envelope-leak strip + dangling-ref advisory.

Regression cover for the defect class closed 2026-08-07 (backlog_1115f9fe35f7,
10 confirmed instances across 4 callers): a malformed client emission
serialized the tool-call envelope into a text param, swallowing the sibling
handoff_notes into the learning body.

The pins that matter:
  - ENVELOPE-TAIL requirement. The first cut keyed on the bare closing tag and
    would have truncated the 40+ DISCUSSION docs that quote the leak pattern in
    prose (the remediation threads themselves). The 08-07 fix required the body
    to END in a closing token, which then MISSED a truncated emission whose tail
    is an unterminated parameter (08-08). Current rule: the field's own closing
    tag must be followed immediately by more ENVELOPE — prose after it means
    discussion, not corruption.
  - Strip-and-REROUTE, never reject: every observed leak carried substantive
    swallowed content, so rejecting the write destroys real data.
  - The ref advisory is advisory: unresolvable ids are reported, never fatal.
"""

import pytest

from shared_memory.write_lint import (
    advisory_payload,
    extract_refs,
    find_unresolved_refs,
    recover_envelope_leak,
    strip_envelope_leak,
)


class TestStripEnvelopeLeak:
    def test_canonical_leak_strips_and_reroutes(self):
        """The 8-instance corpus shape: </learnings> then a handoff param."""
        body = (
            "Real learning content about DryRun.</learnings>\n"
            '<parameter name="handoff_notes">START HERE: read the brief. '
            "BLOCKED ON TOM: pipeline choice.</handoff_notes>\n</invoke>"
        )
        clean, extracted, leaked = strip_envelope_leak(body, "learnings")
        assert leaked is True
        assert clean == "Real learning content about DryRun."
        assert extracted == {
            "handoff_notes": "START HERE: read the brief. BLOCKED ON TOM: pipeline choice."
        }

    def test_instance_nine_shape_closing_parameter_then_invoke(self):
        """Variant seen on the 10th instance: </parameter> before </invoke>."""
        body = (
            "pointer digest text</learnings>"
            '<parameter name="handoff_notes">PRODUCTION: web healthy</parameter></invoke>'
        )
        clean, extracted, leaked = strip_envelope_leak(body, "learnings")
        assert leaked is True
        assert clean == "pointer digest text"
        assert extracted["handoff_notes"] == "PRODUCTION: web healthy"

    def test_discussion_doc_quoting_the_pattern_is_untouched(self):
        """REGRESSION: the remediation threads quote the leak shape in prose.

        40+ such docs exist. Keying on the bare tag truncated them at the
        quote; a real leak has ENVELOPE immediately after the tag, prose does not.
        """
        body = (
            "The defect shape: the body ends with </learnings> then a "
            '<parameter name="handoff_notes">...</handoff_notes> block. '
            "The lint strips it and re-routes the handoff. Normal prose follows."
        )
        clean, extracted, leaked = strip_envelope_leak(body, "learnings")
        assert leaked is False
        assert clean == body
        assert extracted == {}

    def test_unterminated_parameter_tail_is_a_leak(self):
        """REGRESSION (2026-08-08, learning_24b33b8aa7ff16f1 + f588ce30c5b5c9a4):
        a truncated emission whose tail is an UNTERMINATED parameter ending in a
        bare value. The 08-07 'body must END in a closing token' rule missed
        this entirely — the body ends in `nimbus`, not a tag."""
        body = (
            "Real correction content about bridges.</details>\n"
            '<parameter name="project">nimbus'
        )
        clean, extracted, leaked = strip_envelope_leak(body, "details")
        assert leaked is True, "unterminated-parameter tail must be detected"
        assert clean == "Real correction content about bridges."
        assert extracted.get("project") == "nimbus"

    def test_bare_closing_tag_at_end_is_a_leak(self):
        """Field closing tag with nothing after it — still corrupt."""
        body = "Some learning content.</learnings>"
        clean, _, leaked = strip_envelope_leak(body, "learnings")
        assert leaked is True
        assert clean == "Some learning content."

    def test_clean_body_with_unrelated_markup_untouched(self):
        body = "Normal body with <code> and </div> tags in it."
        clean, extracted, leaked = strip_envelope_leak(body, "learnings")
        assert leaked is False
        assert clean == body
        assert extracted == {}

    def test_embedded_field_named_tag_followed_by_markup_is_not_a_leak(self):
        """REGRESSION (coordinator@nimbus, 2026-08-26): content/description are
        real XML tag names (Atom <content>, legacy socket-protocol docs). A body
        legitimately embedding `</content><author>…` matched the field close, but
        the tail (`<author>`) is NOT envelope, so it must NOT be treated as a
        leak — the old bare-`<` tail-guard truncated it silently."""
        body = "Feed item body.</content><author>Jane</author> more text."
        # self-scan (writing content) must not fire
        clean, extracted, leaked = strip_envelope_leak(body, "content")
        assert leaked is False and clean == body and extracted == {}
        # cross-sibling (writing details, content in the pool) must not fire either
        clean2, _, leaked2 = strip_envelope_leak(body, "details")
        assert leaked2 is False and clean2 == body

    def test_field_named_tag_followed_by_real_envelope_still_a_leak(self):
        """The tightened guard must NOT weaken real detection: `</content>` then
        an actual <parameter> envelope is still a leak, project recovered."""
        body = ('Real spec body.</content>'
                '<parameter name="project">nimbus</parameter></invoke>')
        clean, extracted, leaked = strip_envelope_leak(body, "content")
        assert leaked is True
        assert clean == "Real spec body."
        assert extracted.get("project") == "nimbus"

    def test_empty_body_is_safe(self):
        assert strip_envelope_leak("", "learnings") == ("", {}, False)
        assert strip_envelope_leak(None, "learnings") == (None, {}, False)

    def test_handoff_field_guards_its_own_tag(self):
        body = "Handoff text.</handoff_notes></invoke>"
        clean, _, leaked = strip_envelope_leak(body, "handoff_notes")
        assert leaked is True
        assert clean == "Handoff text."


class TestExtractRefs:
    def test_extracts_distinct_refs_in_order(self):
        text = (
            "see backlog_1115f9fe35f7 and msg_b4545521c8e1, "
            "again msg_b4545521c8e1, plus learning_264457e22db85dd0"
        )
        assert extract_refs(text) == [
            "backlog_1115f9fe35f7",
            "msg_b4545521c8e1",
            "learning_264457e22db85dd0",
        ]

    def test_ignores_non_ref_words(self):
        assert extract_refs("backlog_ZZZ msg_ learning") == []

    def test_empty_input(self):
        assert extract_refs("") == []
        assert extract_refs(None) == []


class TestAdvisoryPayload:
    def test_shape_carries_ids_and_verify_guidance(self):
        payload = advisory_payload(["backlog_deadbeef1234"])
        assert payload["unresolved_refs"] == ["backlog_deadbeef1234"]
        assert "memory_get_by_id" in payload["unresolved_refs_note"]
        # Must say it's advisory — the write already succeeded.
        assert "advisory" in payload["unresolved_refs_note"].lower()


class _FakeIdCollection:
    """Chroma-shaped: get(ids=...) returns only the ids it knows."""

    def __init__(self, known):
        self.known = set(known)
        self.name = "fake"

    async def get(self, ids=None, include=None):
        return {"ids": [i for i in (ids or []) if i in self.known]}


class _FakeMongo:
    def __init__(self, known_msgs):
        self.messages = self
        self._known = set(known_msgs)

    def find_one(self, query, projection=None):
        return {"_id": query["_id"]} if query.get("_id") in self._known else None


@pytest.mark.asyncio
class TestFindUnresolvedRefs:
    async def test_no_refs_returns_empty(self):
        assert await find_unresolved_refs("no ids here", None, None, "junto") == []

    async def test_unknown_message_id_is_reported(self):
        db = _FakeMongo(known_msgs=["msg_aaaaaaaaaaaa"])
        out = await find_unresolved_refs(
            "cites msg_aaaaaaaaaaaa and msg_bbbbbbbbbbbb", db, None, "junto"
        )
        assert out == ["msg_bbbbbbbbbbbb"]

    async def test_advisory_never_raises_on_lookup_failure(self):
        """Fail-quiet contract: an infra hiccup must not surface a false alarm."""

        class _Boom:
            messages = None

            def __getattr__(self, _):
                raise RuntimeError("mongo down")

        out = await find_unresolved_refs("msg_aaaaaaaaaaaa", _Boom(), None, "junto")
        assert out == []


class TestRoutingRecovery:
    """The swallowed-`project` chain (legacy-team, 2026-08-09).

    A malformed emission put `project` INSIDE the details body, so the server
    never received it: the doc was filed to shared_patterns with project:"",
    and the dangling-ref advisory then narrowed to shared-only and false-fired
    on every project-scoped id in the same write. One root cause, two silent
    symptoms — and the misfile made a later project-scoped change_status fail
    with an error that reads exactly like a bad doc id.
    """

    def test_swallowed_project_param_is_recoverable(self):
        body = (
            "A dead module in a live tree is indistinguishable from a live one."
            '</details>\n<parameter name="project">nimbus'
        )
        clean, extracted, leaked = strip_envelope_leak(body, "details")
        assert leaked is True
        assert extracted.get("project") == "nimbus", (
            "the routing param must be recoverable — it is what filed the doc "
            "to the wrong collection"
        )
        assert "</details>" not in clean
        assert clean.endswith("live one.")

    def test_multiple_swallowed_params_all_recovered(self):
        body = (
            "Body text.</details>\n"
            '<parameter name="project">nimbus\n'
            '<parameter name="tags">["a","b"]'
        )
        _, extracted, leaked = strip_envelope_leak(body, "details")
        assert leaked is True
        assert extracted.get("project") == "nimbus"
        assert extracted.get("tags") == '["a","b"]'


class TestRecoverEnvelopeLeak:
    """The shared recovery posture every free-text writer routes through
    (backlog_8d33a63e2626): strip + re-route + warn, uniform across store,
    add_backlog_item, define_spec and record_learning."""

    def test_clean_body_is_untouched_and_silent(self):
        body = "A normal body.\n\nDetails here."
        assert recover_envelope_leak(body, "content", "junto") == (body, "junto", [])

    def test_none_and_empty_body_are_safe(self):
        assert recover_envelope_leak("", "content", "junto") == ("", "junto", [])
        assert recover_envelope_leak(None, "content", None) == (None, None, [])

    def test_swallowed_project_recovered_when_caller_passed_none(self):
        body = 'Real body.</content><parameter name="project">nimbus</parameter></invoke>'
        clean, project, notes = recover_envelope_leak(body, "content", None)
        assert project == "nimbus"
        assert "</content>" not in clean and clean.startswith("Real body.")
        assert any("RECOVERED ROUTING" in n for n in notes)

    def test_explicit_project_wins_over_swallowed(self):
        body = 'Real body.</content><parameter name="project">nimbus</parameter></invoke>'
        clean, project, notes = recover_envelope_leak(body, "content", "junto")
        assert project == "junto"
        assert any("explicit one wins" in n for n in notes)

    def test_non_project_param_kept_in_doc(self):
        """A swallowed spec_type (define_spec's old reject case) is preserved
        in-doc under a marked heading rather than silently defaulting."""
        body = (
            'Spec body.</content>'
            '<parameter name="spec_type">agent_state</parameter></invoke>'
        )
        clean, _, notes = recover_envelope_leak(body, "content", "junto")
        assert "## [write-lint] recovered spec_type" in clean
        assert "agent_state" in clean
        assert any("recovered 'spec_type' block kept in-doc" in n for n in notes)

    def test_discussion_prose_is_not_a_leak(self):
        body = "The lint strips a body ending in </content> then a param block."
        assert recover_envelope_leak(body, "content", "junto") == (body, "junto", [])

    def test_description_field_name_for_backlog(self):
        body = 'Do the thing.</description><parameter name="project">sage</parameter></invoke>'
        clean, project, notes = recover_envelope_leak(body, "description", None)
        assert project == "sage" and clean.startswith("Do the thing.")

    def test_embedded_markup_survives_recovery_untouched(self):
        """The FP fix at the recovery layer: a body embedding `</content><tag>`
        is returned verbatim, not truncated, with no spurious recovery notes."""
        body = "Feed body.</content><author>Jane</author> tail text."
        clean, project, notes = recover_envelope_leak(body, "content", "junto")
        assert clean == body and project == "junto" and notes == []
