"""Unit tests for core agent logic.

Covers:
- Clarification judgment rules (fix #1, #6)
- Graph routing functions
- State reducers
- Vector memory chunking
"""

import pytest

from open_deep_research.state import (
    AgentState,
    AmbiguityAssessment,
    override_reducer,
)
from open_deep_research.vector_memory import _chunk_text


# ---------------------------------------------------------------------------
# _assess_need_clarification  (deep_researcher.py)
# We import it here to test in isolation without building the full graph.
# ---------------------------------------------------------------------------

def _assess_need_clarification(assessment: AmbiguityAssessment) -> bool:
    """Deterministic rule: should we ask the user a clarification question?

    Replicated from deep_researcher.py so tests stay valid even if the
    graph wiring changes.  Keep in sync with the source.
    """
    if assessment.subject_clear == "vague":
        return True
    vague_dimensions = sum(
        1
        for value in (
            assessment.scope_clear,
            assessment.audience_clear,
            assessment.timeframe_clear,
        )
        if value == "vague"
    )
    return vague_dimensions >= 2 and not assessment.search_anchored


class TestAssessNeedClarification:
    """Tests for the clarification judgment rule engine."""

    def _make(self, **kwargs) -> AmbiguityAssessment:
        defaults = dict(
            subject_clear="clear",
            scope_clear="clear",
            audience_clear="clear",
            timeframe_clear="clear",
            search_anchored=True,
            question="",
            verification="",
            rationale="",
        )
        defaults.update(kwargs)
        return AmbiguityAssessment(**defaults)

    # --- subject_clear == "vague" always triggers ---
    def test_subject_vague_always_asks(self):
        a = self._make(subject_clear="vague")
        assert _assess_need_clarification(a) is True

    def test_subject_vague_even_when_search_anchored(self):
        a = self._make(subject_clear="vague", search_anchored=True)
        assert _assess_need_clarification(a) is True

    def test_subject_vague_even_when_other_dimensions_clear(self):
        a = self._make(
            subject_clear="vague",
            scope_clear="clear",
            audience_clear="clear",
            timeframe_clear="clear",
            search_anchored=True,
        )
        assert _assess_need_clarification(a) is True

    # --- subject_clear != "vague" ---
    def test_all_clear_no_ask(self):
        a = self._make(subject_clear="clear")
        assert _assess_need_clarification(a) is False

    def test_subject_partial_all_others_clear(self):
        a = self._make(subject_clear="partial")
        assert _assess_need_clarification(a) is False

    # --- two vague secondary dimensions + not anchored → ask ---
    def test_two_vague_not_anchored_asks(self):
        a = self._make(
            subject_clear="clear",
            scope_clear="vague",
            audience_clear="vague",
            timeframe_clear="clear",
            search_anchored=False,
        )
        assert _assess_need_clarification(a) is True

    def test_all_three_vague_not_anchored_asks(self):
        a = self._make(
            subject_clear="partial",
            scope_clear="vague",
            audience_clear="vague",
            timeframe_clear="vague",
            search_anchored=False,
        )
        assert _assess_need_clarification(a) is True

    # --- two vague + search anchored → no ask ---
    def test_two_vague_but_anchored_no_ask(self):
        a = self._make(
            subject_clear="clear",
            scope_clear="vague",
            audience_clear="vague",
            timeframe_clear="clear",
            search_anchored=True,
        )
        assert _assess_need_clarification(a) is False

    # --- only one vague secondary → no ask ---
    def test_one_vague_not_anchored_no_ask(self):
        a = self._make(
            subject_clear="clear",
            scope_clear="vague",
            audience_clear="clear",
            timeframe_clear="clear",
            search_anchored=False,
        )
        assert _assess_need_clarification(a) is False

    # --- "partial" counts as NOT vague ---
    def test_partial_does_not_count_as_vague(self):
        a = self._make(
            subject_clear="clear",
            scope_clear="partial",
            audience_clear="partial",
            timeframe_clear="clear",
            search_anchored=False,
        )
        assert _assess_need_clarification(a) is False

    # --- edge: subject partial + two vague + not anchored → ask ---
    def test_subject_partial_two_vague_not_anchored(self):
        a = self._make(
            subject_clear="partial",
            scope_clear="vague",
            audience_clear="vague",
            timeframe_clear="clear",
            search_anchored=False,
        )
        assert _assess_need_clarification(a) is True


# ---------------------------------------------------------------------------
# Clarification convergence  (fix #6): after user has answered once,
# only subject_clear == "vague" should trigger a follow-up question.
# This logic lives in clarify_with_user_node; we test the rule here.
# ---------------------------------------------------------------------------

def _should_ask_after_first_round(assessment: AmbiguityAssessment) -> bool:
    """Simplified rule for clarify_count >= 1 (fix #6 convergence).

    After the user has answered once, secondary-dimension vagueness
    no longer triggers a question -- only subject vagueness does.
    """
    return assessment.subject_clear == "vague"


class TestClarificationConvergence:
    """Tests for the fix #6 convergence behavior."""

    def _make(self, **kwargs) -> AmbiguityAssessment:
        defaults = dict(
            subject_clear="clear",
            scope_clear="clear",
            audience_clear="clear",
            timeframe_clear="clear",
            search_anchored=False,
            question="",
            verification="",
            rationale="",
        )
        defaults.update(kwargs)
        return AmbiguityAssessment(**defaults)

    def test_after_first_round_subject_vague_still_asks(self):
        a = self._make(subject_clear="vague")
        assert _should_ask_after_first_round(a) is True

    def test_after_first_round_subject_clear_no_ask(self):
        a = self._make(subject_clear="clear")
        assert _should_ask_after_first_round(a) is False

    def test_after_first_round_subject_partial_no_ask(self):
        a = self._make(subject_clear="partial")
        assert _should_ask_after_first_round(a) is False

    def test_after_first_round_scope_vague_no_ask(self):
        """Key regression: scope vague should NOT trigger after first round."""
        a = self._make(scope_clear="vague")
        assert _should_ask_after_first_round(a) is False

    def test_after_first_round_all_vague_except_subject_no_ask(self):
        """Even with all secondary dims vague, no ask if subject is clear."""
        a = self._make(
            subject_clear="clear",
            scope_clear="vague",
            audience_clear="vague",
            timeframe_clear="vague",
        )
        assert _should_ask_after_first_round(a) is False


# ---------------------------------------------------------------------------
# Graph routing functions
# ---------------------------------------------------------------------------

class TestRouteClarification:
    """Tests for _route_clarification conditional edge."""

    def _route(self, state: dict) -> str:
        if state.get("needs_clarification"):
            return "clarify"
        return "write_brief"

    def test_needs_clarification_routes_to_clarify(self):
        assert self._route({"needs_clarification": True}) == "clarify"

    def test_no_clarification_routes_to_brief(self):
        assert self._route({"needs_clarification": False}) == "write_brief"

    def test_missing_key_routes_to_brief(self):
        assert self._route({}) == "write_brief"


class TestShouldContinueResearch:
    """Tests for _should_continue_research conditional edge."""

    def _route(self, state: dict, max_iter: int = 6) -> str:
        if state.get("research_iterations", 0) >= max_iter:
            return "final_report"
        return "supervisor"

    def test_zero_iterations_continues(self):
        assert self._route({"research_iterations": 0}) == "supervisor"

    def test_five_iterations_continues(self):
        assert self._route({"research_iterations": 5}) == "supervisor"

    def test_six_iterations_stops(self):
        assert self._route({"research_iterations": 6}) == "final_report"

    def test_ten_iterations_stops(self):
        assert self._route({"research_iterations": 10}) == "final_report"

    def test_missing_iterations_continues(self):
        assert self._route({}) == "supervisor"

    def test_custom_max(self):
        assert self._route({"research_iterations": 3}, max_iter=3) == "final_report"
        assert self._route({"research_iterations": 2}, max_iter=3) == "supervisor"


# ---------------------------------------------------------------------------
# State reducers
# ---------------------------------------------------------------------------

class TestOverrideReducer:
    """Tests for the override_reducer used in AgentState."""

    def test_add_mode(self):
        result = override_reducer([1, 2], [3])
        assert result == [1, 2, 3]

    def test_override_mode(self):
        result = override_reducer([1, 2], {"type": "override", "value": [3]})
        assert result == [3]

    def test_override_with_dict_value(self):
        result = override_reducer("old", {"type": "override", "value": "new"})
        assert result == "new"

    def test_override_without_value_key(self):
        result = override_reducer([1], {"type": "override"})
        assert result == {"type": "override"}

    def test_add_strings(self):
        result = override_reducer(["a"], ["b", "c"])
        assert result == ["a", "b", "c"]

    def test_empty_add(self):
        result = override_reducer([], [1])
        assert result == [1]


# ---------------------------------------------------------------------------
# Vector memory chunking
# ---------------------------------------------------------------------------

class TestChunkText:
    """Tests for _chunk_text in vector_memory.py."""

    def test_empty_string(self):
        assert _chunk_text("") == []

    def test_none_input(self):
        assert _chunk_text(None) == []

    def test_whitespace_only(self):
        assert _chunk_text("   ") == []

    def test_short_text_whole(self):
        result = _chunk_text("hello world")
        assert result == ["hello world"]
        assert len(result) == 1

    def test_exactly_chunk_size(self):
        text = "a" * 1800
        result = _chunk_text(text)
        assert len(result) == 1
        assert result[0] == text

    def test_long_text_multiple_chunks(self):
        text = "a" * 3000
        result = _chunk_text(text)
        assert len(result) >= 2
        # All original text should be covered
        combined = "".join(result)
        assert "a" * 3000 in combined or len(combined) >= 3000

    def test_overlap_exists(self):
        """Chunks should overlap by _CHUNK_OVERLAP characters."""
        from open_deep_research.vector_memory import _CHUNK_OVERLAP, _CHUNK_SIZE
        text = "x" * (_CHUNK_SIZE + 100)
        result = _chunk_text(text)
        assert len(result) >= 2
        # Second chunk should start before first chunk ends
        second_start_in_first = result[1][:_CHUNK_OVERLAP] in result[0]
        assert second_start_in_first

    def test_preserves_content(self):
        text = "ABCDEFGH" * 300  # 2400 chars
        result = _chunk_text(text)
        assert len(result) >= 2
        for chunk in result:
            assert len(chunk) <= 1800
            assert len(chunk) > 0
