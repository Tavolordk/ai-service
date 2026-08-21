from fastapi.testclient import TestClient

from app.analyzer import ExactGraphAnalyzer, ExactProfileAnalyzer
from app.main import app
from app.models import AnswerRequest, GraphPayload, ProfilePayload


def sample_profile() -> ProfilePayload:
    return ProfilePayload.model_validate(
        {
            'profileId': 'p-1',
            'profileVersionId': 'v-1',
            'versionNumber': 1,
            'searchId': 's-1',
            'preconsolidatedResultId': 'pr-1',
            'effectiveAtUtc': '2026-08-21T00:00:00Z',
            'contractVersion': 'ignored-by-ai',
            'data': [
                {
                    'dataId': 'd1',
                    'code': 'NOMBRE',
                    'value': 'ANA',
                    'origins': [{'originId': 'o1', 'sourceCode': 'SRC1', 'sourceRecordId': '1'}],
                },
                {
                    'dataId': 'd2',
                    'code': 'RFC',
                    'value': 'AAA010101AAA',
                    'origins': [{'originId': 'o1', 'sourceCode': 'SRC1', 'sourceRecordId': '1'}],
                },
                {
                    'dataId': 'd3',
                    'code': 'RFC',
                    'value': 'BBB010101BBB',
                    'origins': [{'originId': 'o2', 'sourceCode': 'SRC2', 'sourceRecordId': '2'}],
                },
            ],
            'addresses': [],
        }
    )


def test_profile_analysis_is_deterministic_and_exact():
    response = ExactProfileAnalyzer().analyze(sample_profile())
    assert response.profileId == 'p-1'
    assert response.title == 'ANA'
    assert response.metrics['conflicts'] == 1
    assert response.conflicts[0].field == 'Rfc'
    assert response.conflicts[0].values == ['AAA010101AAA', 'BBB010101BBB']
    assert 'peligrosidad' in response.guardrails[-1]


def test_answer_uses_received_payload_only():
    request = AnswerRequest(profile=sample_profile(), question='¿Hay inconsistencias?')
    response = ExactProfileAnalyzer().answer(request.question, request.profile, request.selectedNode)
    assert 'AAA010101AAA / BBB010101BBB' in response.text
    assert len(response.evidence) == 2


def test_graph_analysis_does_not_invent_links():
    payload = GraphPayload.model_validate(
        {
            'nodes': [
                {'id': 'a', 'type': 'person', 'title': 'A'},
                {'id': 'b', 'type': 'source', 'title': 'B'},
                {'id': 'c', 'type': 'source', 'title': 'C'},
            ],
            'links': [{'id': 'ab', 'sourceId': 'a', 'targetId': 'b'}],
            'selectedNodeId': 'a',
        }
    )
    response = ExactGraphAnalyzer().analyze(payload)
    assert response.nodeCount == 3
    assert response.linkCount == 1
    assert response.componentCount == 2
    assert response.connectedNodeIds == ['b']


def test_api_rejects_system_credentials():
    client = TestClient(app)
    response = client.get('/health', headers={'Authorization': 'Bearer should-not-arrive'})
    assert response.status_code == 400


def test_api_security_headers_and_no_store():
    client = TestClient(app)
    response = client.get('/health')
    assert response.status_code == 200
    assert response.headers['cache-control'].startswith('no-store')
    assert response.headers['x-frame-options'] == 'DENY'
    assert response.json()['mode'] == 'local-private-chat'


def test_story_answer_is_conversational_and_graph_grounded():
    profile = sample_profile()
    graph = GraphPayload.model_validate(
        {
            'nodes': [
                {'id': 'p-1', 'type': 'Perfil', 'title': 'ANA'},
                {'id': 'src-1', 'type': 'Fuente', 'title': 'RNSP'},
                {'id': 'src-2', 'type': 'Fuente', 'title': 'Vehículos'},
            ],
            'links': [
                {'id': 'l1', 'sourceId': 'p-1', 'targetId': 'src-1'},
                {'id': 'l2', 'sourceId': 'p-1', 'targetId': 'src-2'},
            ],
            'selectedNodeId': 'p-1',
        }
    )
    response = ExactProfileAnalyzer().answer('', profile, None, graph)
    assert 'Mira, de ANA hay bastante información para seguirle el hilo' in response.text
    assert 'tiene 2 conexiones directas' in response.text
    assert 'RNSP' in response.text
    assert 'Vehículos' in response.text
    assert 'amistad' in response.disclaimer


def test_answer_endpoint_accepts_graph_context_without_credentials():
    client = TestClient(app)
    response = client.post(
        '/api/v1/intelligence/profile/answer',
        headers={'Origin': 'http://localhost:4202'},
        json={
            'profile': sample_profile().model_dump(),
            'question': 'Cuéntame todo lo que aparece de esta persona',
            'selectedNode': {'id': 'p-1', 'type': 'Perfil', 'title': 'ANA'},
            'graph': {
                'nodes': [
                    {'id': 'p-1', 'type': 'Perfil', 'title': 'ANA'},
                    {'id': 'src-1', 'type': 'Fuente', 'title': 'RNSP'},
                ],
                'links': [{'id': 'l1', 'sourceId': 'p-1', 'targetId': 'src-1'}],
                'selectedNodeId': 'p-1',
            },
        },
    )
    assert response.status_code == 200
    assert 'Mira, de ANA hay bastante información para seguirle el hilo' in response.json()['text']
    assert response.headers.get('access-control-allow-origin') == 'http://localhost:4202'


def test_chat_grounding_uses_only_received_evidence():
    from app.config import settings
    from app.grounded_chat import select_grounding
    from app.models import ChatRequest

    request = ChatRequest(
        profile=sample_profile(),
        question='¿Cuál RFC aparece y qué fuente lo reporta?',
        history=[],
    )
    grounding = select_grounding(request, settings)
    joined = '\n'.join(item.context_text for item in grounding)
    assert 'AAA010101AAA' in joined
    assert 'BBB010101BBB' in joined
    assert 'SRC1' in joined
    assert 'SRC2' in joined
    assert 'datos externos' not in joined


def test_chat_engine_streams_local_llm_and_maps_citations():
    from app.config import settings
    from app.grounded_chat import GroundedChatEngine
    from app.models import ChatRequest

    class FakeLlm:
        def stream_chat(self, messages):
            assert messages[0]['role'] == 'system'
            assert 'CONTEXTO VERIFICADO' in messages[0]['content']
            yield 'El RFC aparece con dos valores en las fuentes '
            yield '[E3] [E4].'

    request = ChatRequest(
        profile=sample_profile(),
        question='¿Qué RFC aparece?',
        history=[],
    )
    events = list(GroundedChatEngine(settings, FakeLlm()).stream(request))
    assert events[0]['type'] == 'meta'
    assert any(item['type'] == 'delta' for item in events)
    done = events[-1]
    assert done['type'] == 'done'
    assert done['mode'] == 'local-llm'
    assert len(done['evidence']) == 2


def test_chat_falls_back_without_exposing_external_services():
    from app.config import settings
    from app.grounded_chat import GroundedChatEngine
    from app.llm_local import LocalLlmError
    from app.models import ChatRequest

    class OfflineLlm:
        def stream_chat(self, messages):
            raise LocalLlmError('offline')
            yield ''  # pragma: no cover

    request = ChatRequest(
        profile=sample_profile(),
        question='¿Hay diferencias?',
        history=[],
    )
    events = list(GroundedChatEngine(settings, OfflineLlm()).stream(request))
    replacement = next(item for item in events if item['type'] == 'replace')
    assert 'AAA010101AAA / BBB010101BBB' in replacement['text']
    assert events[-1]['mode'] == 'deterministic-fallback'


def sample_profile_with_addresses() -> ProfilePayload:
    payload = sample_profile().model_dump()
    payload['addresses'] = [
        {
            'addressId': 'a1',
            'type': 'Casa',
            'street': 'PRIMERA',
            'exteriorNumber': '10',
            'neighborhood': 'CENTRO',
            'municipality': 'ACAPULCO',
            'state': 'GUERRERO',
            'postalCode': '39000',
            'origins': [{'originId': 'oa1', 'sourceCode': 'SRC-A', 'sourceRecordId': 'A-1'}],
        },
        {
            'addressId': 'a2',
            'type': 'Trabajo',
            'street': 'SEGUNDA',
            'exteriorNumber': '20',
            'neighborhood': 'PROGRESO',
            'municipality': 'ACAPULCO',
            'state': 'GUERRERO',
            'postalCode': '39350',
            'origins': [{'originId': 'oa2', 'sourceCode': 'SRC-B', 'sourceRecordId': 'B-1'}],
        },
        {
            'addressId': 'a3',
            'type': 'Otro',
            'street': 'TERCERA',
            'exteriorNumber': '30',
            'neighborhood': 'COSTA AZUL',
            'municipality': 'ACAPULCO',
            'state': 'GUERRERO',
            'postalCode': '39850',
            'origins': [{'originId': 'oa3', 'sourceCode': 'SRC-C', 'sourceRecordId': 'C-1'}],
        },
    ]
    return ProfilePayload.model_validate(payload)


def test_first_address_returns_only_first_address_in_fallback():
    profile = sample_profile_with_addresses()
    response = ExactProfileAnalyzer().answer('Dame la primera dirección', profile)
    assert 'PRIMERA 10' in response.text
    assert 'SEGUNDA 20' not in response.text
    assert 'TERCERA 30' not in response.text
    assert len(response.evidence) == 1
    assert response.evidence[0].id == 'a1'


def test_requested_address_can_include_only_its_sources():
    profile = sample_profile_with_addresses()
    response = ExactProfileAnalyzer().answer('¿De qué fuente sale la segunda dirección?', profile)
    assert 'SEGUNDA 20' in response.text
    assert 'SRC-B' in response.text
    assert 'SRC-A' not in response.text
    assert 'SRC-C' not in response.text
    assert [item.id for item in response.evidence] == ['a2']


def test_chat_grounding_filters_exact_ordinal_address():
    from app.config import settings
    from app.grounded_chat import build_query_plan, select_grounding
    from app.models import ChatRequest

    request = ChatRequest(
        profile=sample_profile_with_addresses(),
        question='Dame la primera dirección',
        history=[],
    )
    plan = build_query_plan(request)
    grounding = select_grounding(request, settings, plan)
    values = '\n'.join(item.evidence.value for item in grounding)
    assert plan.domain == 'address'
    assert plan.ordinal_index == 0
    assert 'PRIMERA 10' in values
    assert 'SEGUNDA 20' not in values
    assert 'TERCERA 30' not in values
    assert len([item for item in grounding if item.evidence.kind == 'address']) == 1


def test_followup_second_inherits_address_intent_from_chat_history():
    from app.config import settings
    from app.grounded_chat import build_query_plan, select_grounding
    from app.models import ChatHistoryMessage, ChatRequest

    request = ChatRequest(
        profile=sample_profile_with_addresses(),
        question='¿Y la segunda?',
        history=[
            ChatHistoryMessage(role='user', content='Dame la primera dirección'),
            ChatHistoryMessage(role='assistant', content='La primera dirección es...'),
        ],
    )
    plan = build_query_plan(request)
    grounding = select_grounding(request, settings, plan)
    values = '\n'.join(item.evidence.value for item in grounding)
    assert plan.domain == 'address'
    assert plan.ordinal_index == 1
    assert 'SEGUNDA 20' in values
    assert 'PRIMERA 10' not in values
    assert 'TERCERA 30' not in values


def test_out_of_range_address_does_not_substitute_another():
    profile = sample_profile_with_addresses()
    response = ExactProfileAnalyzer().answer('Dame la cuarta dirección', profile)
    assert 'sólo trae 3' in response.text
    assert 'PRIMERA 10' not in response.text
    assert 'SEGUNDA 20' not in response.text
    assert 'TERCERA 30' not in response.text


def test_compact_backend_codes_match_human_field_questions():
    payload = sample_profile().model_dump()
    payload['data'].extend([
        {
            'dataId': 'd4',
            'code': 'FECHANACIMIENTO',
            'value': '1990-01-02',
            'origins': [{'originId': 'o4', 'sourceCode': 'SRC4', 'sourceRecordId': '4'}],
        },
        {
            'dataId': 'd5',
            'code': 'APELLIDOPATERNO',
            'value': 'PEREZ',
            'origins': [{'originId': 'o5', 'sourceCode': 'SRC5', 'sourceRecordId': '5'}],
        },
    ])
    profile = ProfilePayload.model_validate(payload)

    birth = ExactProfileAnalyzer().answer('¿Cuál es su fecha de nacimiento?', profile)
    assert '1990-01-02' in birth.text
    assert [item.value for item in birth.evidence] == ['1990-01-02']

    surname = ExactProfileAnalyzer().answer('Dame el apellido paterno', profile)
    assert 'PEREZ' in surname.text
    assert [item.value for item in surname.evidence] == ['PEREZ']


def test_chat_grounding_matches_compact_backend_code_for_exact_field():
    from app.config import settings
    from app.grounded_chat import build_query_plan, select_grounding
    from app.models import ChatRequest

    payload = sample_profile().model_dump()
    payload['data'].append(
        {
            'dataId': 'd4',
            'code': 'FECHANACIMIENTO',
            'value': '1990-01-02',
            'origins': [{'originId': 'o4', 'sourceCode': 'SRC4', 'sourceRecordId': '4'}],
        }
    )
    profile = ProfilePayload.model_validate(payload)
    request = ChatRequest(profile=profile, question='¿Cuál es su fecha de nacimiento?', history=[])
    plan = build_query_plan(request)
    grounding = select_grounding(request, settings, plan)
    joined = '\n'.join(item.context_text for item in grounding)

    assert plan.domain == 'data'
    assert '1990-01-02' in joined
    assert 'AAA010101AAA' not in joined
    assert 'BBB010101BBB' not in joined


def test_source_question_retrieves_values_backed_by_that_source():
    from app.config import settings
    from app.grounded_chat import build_query_plan, select_grounding
    from app.models import ChatRequest

    request = ChatRequest(
        profile=sample_profile(),
        question='¿Qué reporta SRC1 de esta persona?',
        history=[],
    )
    plan = build_query_plan(request)
    grounding = select_grounding(request, settings, plan)
    joined = '\n'.join(item.context_text for item in grounding)

    assert plan.domain == 'origin'
    assert 'ANA' in joined
    assert 'AAA010101AAA' in joined
    assert 'BBB010101BBB' not in joined


def test_selected_node_question_retrieves_only_direct_graph_context():
    from app.config import settings
    from app.grounded_chat import build_query_plan, select_grounding
    from app.models import ChatRequest, GraphNodeInput

    graph = GraphPayload.model_validate(
        {
            'nodes': [
                {'id': 'p-1', 'type': 'Perfil', 'title': 'ANA'},
                {'id': 'src-1', 'type': 'Fuente', 'title': 'RNSP'},
                {'id': 'src-2', 'type': 'Fuente', 'title': 'VEHICULOS'},
            ],
            'links': [
                {'id': 'l1', 'sourceId': 'p-1', 'targetId': 'src-1'},
                {'id': 'l2', 'sourceId': 'p-1', 'targetId': 'src-2'},
            ],
            'selectedNodeId': 'src-1',
        }
    )
    selected = GraphNodeInput.model_validate({'id': 'src-1', 'type': 'Fuente', 'title': 'RNSP'})
    request = ChatRequest(
        profile=sample_profile(),
        question='¿Qué relación tiene el nodo seleccionado?',
        history=[],
        graph=graph,
        selectedNode=selected,
    )
    plan = build_query_plan(request)
    grounding = select_grounding(request, settings, plan)
    labels = '\n'.join(item.evidence.label for item in grounding)

    assert 'RNSP' in labels
    assert 'ANA' in labels
    assert 'VEHICULOS' not in labels


def test_deterministic_fallback_answers_specific_source_without_dumping_all_sources():
    response = ExactProfileAnalyzer().answer('¿Qué reporta SRC1 de esta persona?', sample_profile())
    assert 'SRC1' in response.text
    assert 'ANA' in response.text
    assert 'AAA010101AAA' in response.text
    assert 'BBB010101BBB' not in response.text
    assert all('SRC2' not in item.sourceCodes for item in response.evidence)


def test_chat_llm_receives_only_requested_first_address_not_all_addresses():
    from app.config import settings
    from app.grounded_chat import GroundedChatEngine
    from app.models import ChatRequest

    class InspectingLlm:
        def stream_chat(self, messages):
            system = messages[0]['content']
            assert 'PRIMERA 10' in system
            assert 'SEGUNDA 20' not in system
            assert 'TERCERA 30' not in system
            yield 'La primera dirección es PRIMERA 10, CENTRO, ACAPULCO, GUERRERO, C.P. 39000 [E5].'

    request = ChatRequest(
        profile=sample_profile_with_addresses(),
        question='Dame la primera dirección',
        history=[],
    )
    events = list(GroundedChatEngine(settings, InspectingLlm()).stream(request))
    done = events[-1]
    assert done['mode'] == 'local-llm'
    assert len(done['evidence']) == 1
    assert done['evidence'][0]['id'] == 'a1'


def test_offline_chat_fallback_keeps_first_address_exact():
    from app.config import settings
    from app.grounded_chat import GroundedChatEngine
    from app.llm_local import LocalLlmError
    from app.models import ChatRequest

    class OfflineLlm:
        def stream_chat(self, messages):
            raise LocalLlmError('offline')
            yield ''  # pragma: no cover

    request = ChatRequest(
        profile=sample_profile_with_addresses(),
        question='Dame la primera dirección',
        history=[],
    )
    events = list(GroundedChatEngine(settings, OfflineLlm()).stream(request))
    replacement = next(item for item in events if item['type'] == 'replace')
    assert 'PRIMERA 10' in replacement['text']
    assert 'SEGUNDA 20' not in replacement['text']
    assert 'TERCERA 30' not in replacement['text']
    assert events[-1]['mode'] == 'deterministic-fallback'
