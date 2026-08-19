"""Graph state definitions and data structures for the Deep Research agent."""

import operator
from typing import Annotated, Literal, Optional

from langchain_core.messages import MessageLikeRepresentation
from langgraph.graph import MessagesState
from pydantic import BaseModel, Field
from typing_extensions import TypedDict


###################
# Structured Outputs
###################
class ConductResearch(BaseModel):
    """Call this tool to conduct research on a specific topic."""
    research_topic: str = Field(
        description="The topic to research. Should be a single topic, and should be described in high detail (at least a paragraph).",
    )

class ResearchComplete(BaseModel):
    """Call this tool to indicate that the research is complete."""

class Summary(BaseModel):
    """Research summary with key findings."""
    
    summary: str
    key_excerpts: str

class SearchQueries(BaseModel):
    """Search queries derived from the user's initial request for the pre-search step."""

    queries: list[str] = Field(
        description="1-3 focused web search queries derived from the user's research request",
    )

class AmbiguityAssessment(BaseModel):
    """Multi-dimension ambiguity scoring used by the clarification judgment node.

    The model only *scores* the request across fixed dimensions; the node decides
    whether to ask the user via deterministic rules on top of these scores.
    """

    subject_clear: Literal["clear", "partial", "vague"] = Field(
        description="Whether the research subject/topic is clearly specified",
    )
    scope_clear: Literal["clear", "partial", "vague"] = Field(
        description="Whether the research scope/boundaries are clearly specified",
    )
    audience_clear: Literal["clear", "partial", "vague"] = Field(
        description="Whether the target audience and depth are clear",
    )
    timeframe_clear: Literal["clear", "partial", "vague"] = Field(
        description="Whether the timeframe is clear",
    )
    search_anchored: bool = Field(
        description="Whether the pre-search results substantively anchor the research subject",
    )
    question: str = Field(
        description="A clarifying question to ask the user if one is needed; empty if not needed",
    )
    verification: str = Field(
        description="A message to show when research starts, listing any assumptions made",
    )
    rationale: str = Field(
        description="One sentence justifying the scores",
    )

class ResearchQuestion(BaseModel):
    """Research question and brief for guiding research."""
    
    research_brief: str = Field(
        description="A research question that will be used to guide the research.",
    )


###################
# State Definitions
###################

def override_reducer(current_value, new_value):
    """Reducer function that allows overriding values in state."""
    if isinstance(new_value, dict) and new_value.get("type") == "override":
        return new_value.get("value", new_value)
    else:
        return operator.add(current_value, new_value)
    
class AgentInputState(MessagesState):
    """InputState is only 'messages'."""

class AgentState(MessagesState):
    """Main agent state containing messages and research data."""
    
    supervisor_messages: Annotated[list[MessageLikeRepresentation], override_reducer]
    research_brief: Optional[str]
    raw_notes: Annotated[list[str], override_reducer] = []
    notes: Annotated[list[str], override_reducer] = []
    final_report: str
    # Clarification judgment state: one-time pre-search context reused across
    # clarification rounds, and a counter bounding how many times we may ask.
    pre_search_context: Optional[str] = None
    clarify_count: int = 0

class SupervisorState(TypedDict):
    """State for the supervisor that manages research tasks."""
    
    supervisor_messages: Annotated[list[MessageLikeRepresentation], override_reducer]
    research_brief: str
    notes: Annotated[list[str], override_reducer] = []
    research_iterations: int = 0
    raw_notes: Annotated[list[str], override_reducer] = []

class ResearcherState(TypedDict):
    """State for individual researchers conducting research."""
    
    researcher_messages: Annotated[list[MessageLikeRepresentation], operator.add]
    tool_call_iterations: int = 0
    research_topic: str
    compressed_research: str
    raw_notes: Annotated[list[str], override_reducer] = []

class ResearcherOutputState(BaseModel):
    """Output state from individual researchers."""
    
    compressed_research: str
    raw_notes: Annotated[list[str], override_reducer] = []