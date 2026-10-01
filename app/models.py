from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class ProfileInput(FlexibleModel):
    profileId: str

    @field_validator("profileId", mode="before")
    @classmethod
    def profile_id_to_str(cls, value: Any) -> str:
        if value is None:
            raise ValueError("profileId es obligatorio")
        return str(value)


class ChatHistoryMessage(FlexibleModel):
    role: Literal["user", "assistant", "system"]
    content: str = Field(min_length=1, max_length=20000)


class ChatRequest(FlexibleModel):
    profile: ProfileInput
    question: str = Field(min_length=1, max_length=12000)
    selectedNode: dict[str, Any] | None = None
    graph: dict[str, Any] | None = None
    history: list[ChatHistoryMessage] = Field(default_factory=list, max_length=24)
    thinking: bool = False
    mode: Literal["quick", "auto", "deep"] = "auto"
    caseId: str | None = None


class CaseChatRequest(FlexibleModel):
    question: str = Field(min_length=1, max_length=12000)
    history: list[ChatHistoryMessage] = Field(default_factory=list, max_length=24)
    thinking: bool = False
    mode: Literal["quick", "auto", "deep"] = "auto"


class ChatResponse(FlexibleModel):
    text: str
    mode: str
    model: str | None = None
    thinking: bool = False
    reasoning: str | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class CaseMetadata(FlexibleModel):
    caseId: str
    bytes: int
    facts: int
    indexedAt: str
    sourceFile: str
    databaseFile: str
