from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .analyzer import ExactGraphAnalyzer, ExactProfileAnalyzer
from .config import settings
from .grounded_chat import GroundedChatEngine, encode_sse
from .llm_local import LocalOllamaClient
from .models import (
    AnalysisResponse,
    AnswerRequest,
    AnswerResponse,
    ChatRequest,
    ChatResponse,
    GraphAnalysisResponse,
    GraphPayload,
    ProfilePayload,
)

app = FastAPI(
    title='Profile Intelligence API',
    version='2.0.0',
    description='Análisis y chat local, privado y basado exclusivamente en evidencia recibida.',
    docs_url='/docs' if settings.enable_docs else None,
    redoc_url='/redoc' if settings.enable_docs else None,
    openapi_url='/openapi.json' if settings.enable_docs else None,
)

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=list(settings.allowed_hosts),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.allowed_origins),
    allow_credentials=False,
    allow_methods=['GET', 'POST', 'OPTIONS'],
    allow_headers=['Content-Type', 'X-Trace-Id'],
    max_age=600,
)

profile_analyzer = ExactProfileAnalyzer()
graph_analyzer = ExactGraphAnalyzer()
llm_client = LocalOllamaClient(settings)
chat_engine = GroundedChatEngine(settings, llm_client)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError):
    # No devolvemos el valor de entrada (`input`) que FastAPI incluye por defecto,
    # porque podría contener CURP, RFC, domicilios u otros datos del perfil.
    errors = [
        {
            'field': '.'.join(str(part) for part in error.get('loc', []) if part != 'body'),
            'type': error.get('type', 'validation_error'),
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={'detail': 'Payload inválido.', 'errors': errors},
    )


@app.middleware('http')
async def privacy_and_security_middleware(request: Request, call_next):
    # El servicio es deliberadamente independiente del sistema de autenticación actual.
    # Rechazar estas cabeceras evita que un JWT/cookie del sistema llegue por accidente.
    if request.headers.get('authorization') or request.headers.get('cookie'):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={'detail': 'No envíe credenciales del sistema a la API de inteligencia.'},
        )

    content_length = request.headers.get('content-length')
    if content_length:
        try:
            if int(content_length) > settings.max_content_length:
                return JSONResponse(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    content={'detail': 'Payload demasiado grande.'},
                )
        except ValueError:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={'detail': 'Content-Length inválido.'},
            )

    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    return response


@app.get('/health')
def health() -> dict[str, str]:
    return {
        'status': 'ok',
        'mode': 'local-private-chat',
        'model': settings.llm_model if settings.llm_enabled else 'disabled',
    }


@app.get('/health/llm')
def health_llm() -> dict[str, str | bool]:
    state = llm_client.status()
    return {
        'status': 'ok' if state.reachable and state.model_available else 'degraded',
        'runtimeReachable': state.reachable,
        'modelAvailable': state.model_available,
        'model': settings.llm_model,
        'detail': state.detail,
    }


@app.post('/api/v1/intelligence/profile/analyze', response_model=AnalysisResponse)
def analyze_profile(profile: ProfilePayload) -> AnalysisResponse:
    return profile_analyzer.analyze(profile)


@app.post('/api/v1/intelligence/profile/answer', response_model=AnswerResponse)
def answer_profile(request: AnswerRequest) -> AnswerResponse:
    # Se conserva el endpoint determinista por compatibilidad/fallback.
    return profile_analyzer.answer(request.question, request.profile, request.selectedNode, request.graph)


@app.post('/api/v1/intelligence/chat', response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    return chat_engine.complete(request)


@app.post('/api/v1/intelligence/chat/stream')
def chat_stream(request: ChatRequest) -> StreamingResponse:
    def events():
        for event in chat_engine.stream(request):
            yield encode_sse(event)

    return StreamingResponse(
        events(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-store, max-age=0',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        },
    )


@app.post('/api/v1/intelligence/graph/analyze', response_model=GraphAnalysisResponse)
def analyze_graph(graph: GraphPayload) -> GraphAnalysisResponse:
    return graph_analyzer.analyze(graph)
