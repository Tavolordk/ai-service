from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class StrictPayloadModel(BaseModel):
    # Se ignoran campos ajenos al contrato de IA para minimizar datos procesados.
    model_config = ConfigDict(extra='ignore', str_strip_whitespace=True)


class Origin(StrictPayloadModel):
    originId: str = ''
    sourceCode: str | None = None
    sourceRecordId: str | None = None


class Datum(StrictPayloadModel):
    dataId: str
    dataType: str | None = None
    code: str | None = None
    value: str | None = None
    origins: list[Origin] | None = None


class Address(StrictPayloadModel):
    addressId: str
    type: str | None = None
    street: str | None = None
    exteriorNumber: str | None = None
    interiorNumber: str | None = None
    neighborhood: str | None = None
    postalCode: str | None = None
    stateId: str | None = None
    state: str | None = None
    municipalityId: str | None = None
    municipality: str | None = None
    origins: list[Origin] | None = None


class ProfilePayload(StrictPayloadModel):
    profileId: str
    profileVersionId: str = ''
    versionNumber: int = 0
    searchId: str = ''
    preconsolidatedResultId: str = ''
    effectiveAtUtc: str = ''
    data: list[Datum] | None = None
    addresses: list[Address] | None = None


class GraphNodeInput(StrictPayloadModel):
    id: str
    type: str
    title: str
    subtitle: str = ''
    details: list[dict[str, Any]] = Field(default_factory=list)


class GraphLinkInput(StrictPayloadModel):
    id: str
    sourceId: str
    targetId: str


class GraphPayload(StrictPayloadModel):
    nodes: list[GraphNodeInput] = Field(default_factory=list)
    links: list[GraphLinkInput] = Field(default_factory=list)
    selectedNodeId: str | None = None


class Evidence(StrictPayloadModel):
    kind: Literal['profile', 'data', 'address', 'origin', 'node', 'relation'] | str
    id: str
    label: str
    value: str
    sourceCodes: list[str] = Field(default_factory=list)


class Conflict(StrictPayloadModel):
    field: str
    values: list[str]
    evidence: list[Evidence]


class AnalysisResponse(StrictPayloadModel):
    profileId: str
    title: str
    summary: str
    metrics: dict[str, int]
    facts: list[Evidence]
    conflicts: list[Conflict]
    recommendations: list[str]
    guardrails: list[str]


class GraphAnalysisResponse(StrictPayloadModel):
    nodeCount: int
    linkCount: int
    componentCount: int
    selectedNodeId: str | None
    selectedDegree: int
    connectedNodeIds: list[str]
    description: str


class AnswerRequest(StrictPayloadModel):
    profile: ProfilePayload
    question: str = Field(default='', max_length=2000)
    selectedNode: GraphNodeInput | None = None
    graph: GraphPayload | None = None


class AnswerResponse(StrictPayloadModel):
    text: str
    evidence: list[Evidence]
    disclaimer: str | None = None


class ChatHistoryMessage(StrictPayloadModel):
    role: Literal['user', 'assistant']
    content: str = Field(min_length=1, max_length=6000)


class ChatRequest(StrictPayloadModel):
    profile: ProfilePayload
    question: str = Field(min_length=1, max_length=3000)
    selectedNode: GraphNodeInput | None = None
    graph: GraphPayload | None = None
    history: list[ChatHistoryMessage] = Field(default_factory=list, max_length=12)


class ChatResponse(StrictPayloadModel):
    text: str
    evidence: list[Evidence]
    disclaimer: str | None = None
    mode: Literal['local-llm', 'deterministic-fallback']
    model: str | None = None
