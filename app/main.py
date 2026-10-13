from __future__ import annotations

import json
from collections.abc import Iterable
from threading import Thread

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .analyzer import PoliceAnalyzer
from .case_store import CaseStore
from .config import settings
from .models import CaseChatRequest, ChatRequest, ChatResponse
from .ollama_client import LlmStreamChunk, LocalLlmError, LocalOllamaClient
from .request_limit import InlineBodyLimit

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
)

app.add_middleware(InlineBodyLimit, max_bytes=settings.max_inline_content_length)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.allowed_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts) + ["*"] if "*" in settings.allowed_hosts else list(settings.allowed_hosts))

case_store = CaseStore(settings)
llm = LocalOllamaClient(settings)
analyzer = PoliceAnalyzer(settings, llm, case_store)


@app.on_event("startup")
def _schedule_warmup() -> None:
    if settings.llm_warmup and settings.llm_enabled:
        Thread(target=llm.warmup, name="ollama-warmup", daemon=True).start()


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"




def _frontend_evidence(items: list[dict]) -> list[dict]:
    kind_by_section = {
        "addresses": "address",
        "sources": "origin",
        "relations": "relation",
        "vehicles": "node",
        "weapons": "node",
        "identity": "data",
        "identifiers": "data",
    }
    out: list[dict] = []
    for index, item in enumerate(items or []):
        if not isinstance(item, dict):
            continue
        section = str(item.get("section") or "data")
        path = str(item.get("path") or "")
        value = item.get("value")
        raw_kind = str(item.get("kind") or "")
        kind = raw_kind if raw_kind in {"profile", "data", "address", "origin", "node", "relation"} else kind_by_section.get(section, "data")
        source_codes = item.get("sourceCodes") if isinstance(item.get("sourceCodes"), list) else []
        out.append({
            "kind": kind,
            "id": str(item.get("id") or path or f"evidence-{index}"),
            "label": str(item.get("label") or section or path or f"Evidencia {index + 1}"),
            "value": "" if value is None else str(value),
            "sourceCodes": [str(code) for code in source_codes if code is not None],
        })
    return out

def _content_text(chunks: Iterable[LlmStreamChunk]) -> str:
    full = ""
    for chunk in chunks:
        if chunk.kind == "content":
            full += chunk.text
        elif chunk.kind == "replace":
            full = chunk.text
    return full.strip()


def _stream_response(*, evidence: list[dict], chunks: Iterable[LlmStreamChunk], thinking: bool, mode: str, model: str | None, route: str):
    def generate():
        yield _sse("meta", {
            "type": "meta",
            "mode": mode,
            "model": model,
            "thinking": thinking,
            "route": route,
        })
        full = ""
        try:
            for chunk in chunks:
                # El razonamiento nunca sale por SSE. En modo directo, si al final se
                # detecta idioma incorrecto o truncamiento, la API sustituye la salida
                # provisional por una respuesta final validada mediante `replace`.
                if chunk.kind == "reasoning":
                    continue
                if chunk.kind == "replace":
                    full = chunk.text
                    yield _sse("replace", {
                        "type": "replace",
                        "text": chunk.text,
                        "mode": mode,
                        "model": model,
                        "thinking": thinking,
                    })
                    continue
                full += chunk.text
                yield _sse("delta", {"type": "delta", "text": chunk.text})

            yield _sse("done", {
                "type": "done",
                "text": full.strip(),
                "mode": mode,
                "model": model,
                "thinking": thinking,
                "route": route,
                "evidence": _frontend_evidence(evidence[: settings.llm_max_evidence]),
            })
        except LocalLlmError as exc:
            yield _sse("error", {"type": "error", "message": str(exc)})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/")
def root():
    return {
        "service": settings.app_name,
        "version": settings.app_version,
        "model": settings.llm_model,
        "status": "ok",
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": settings.llm_model,
        "llmEnabled": settings.llm_enabled,
        "caseLimitBytes": settings.max_case_bytes,
        "maxInlineBytes": settings.max_inline_content_length,
        "indexedCaseMode": True,
        "contextOptimization": True,
        "contextCache": analyzer.cache_stats(),
        "fastPath": analyzer.fast_path_stats(),
        "maxContextChars": settings.context_max_chars,
        "maxContextWindow": settings.llm_num_ctx,
        "llmStats": llm.stats.snapshot(),
    }


@app.post("/api/v1/intelligence/chat/stream")
def intelligence_chat_stream(request: ChatRequest):
    analysis = analyzer.stream_inline(request)
    return _stream_response(
        evidence=analysis.evidence, chunks=analysis.chunks, thinking=analysis.thinking,
        mode=analysis.mode, model=analysis.model, route=analysis.route,
    )


@app.post("/api/v1/intelligence/chat", response_model=ChatResponse)
def intelligence_chat(request: ChatRequest):
    analysis = analyzer.stream_inline(request)
    try:
        text = _content_text(analysis.chunks)
    except LocalLlmError as exc:
        raise HTTPException(503, str(exc)) from exc
    return ChatResponse(
        text=text,
        mode=analysis.mode,
        model=analysis.model,
        thinking=analysis.thinking,
        evidence=_frontend_evidence(analysis.evidence[: settings.llm_max_evidence]),
    )


@app.post("/api/v1/cases")
async def ingest_case(request: Request, caseId: str = Query(min_length=1, max_length=120)):
    metadata = await case_store.ingest(caseId, request)
    return metadata.model_dump()


@app.get("/api/v1/cases/{case_id}/search")
def search_indexed_case(case_id: str, q: str = Query(min_length=1, max_length=1000),
                        limit: int = Query(default=30, ge=1, le=200)):
    return {"caseId": case_id, "items": case_store.search(case_id, q, limit=limit)}


@app.get("/api/v1/cases/{case_id}/links")
def get_indexed_links(case_id: str, nodeId: str = Query(min_length=1, max_length=256),
                      limit: int = Query(default=30, ge=1, le=200),
                      offset: int = Query(default=0, ge=0),
                      afterEdgeId: int | None = Query(default=None, ge=0)):
    return case_store.links(case_id, nodeId, limit=limit, offset=offset, after_edge_id=afterEdgeId)


@app.get("/api/v1/cases/{case_id}")
def get_case(case_id: str):
    return case_store.metadata(case_id).model_dump()


@app.delete("/api/v1/cases/{case_id}", status_code=204)
def delete_case(case_id: str):
    case_store.delete(case_id)
    return JSONResponse(status_code=204, content=None)


@app.post("/api/v1/cases/{case_id}/chat/stream")
def case_chat_stream(case_id: str, request: CaseChatRequest):
    analysis = analyzer.stream_case(case_id, request)
    return _stream_response(
        evidence=analysis.evidence, chunks=analysis.chunks, thinking=analysis.thinking,
        mode=analysis.mode, model=analysis.model, route=analysis.route,
    )


@app.post("/api/v1/cases/{case_id}/chat", response_model=ChatResponse)
def case_chat(case_id: str, request: CaseChatRequest):
    analysis = analyzer.stream_case(case_id, request)
    try:
        text = _content_text(analysis.chunks)
    except LocalLlmError as exc:
        raise HTTPException(503, str(exc)) from exc
    return ChatResponse(
        text=text,
        mode=analysis.mode,
        model=analysis.model,
        thinking=analysis.thinking,
        evidence=_frontend_evidence(analysis.evidence[: settings.llm_max_evidence]),
    )
