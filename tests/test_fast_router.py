from app.context_builder import ContextBuilder
from app.fast_router import FastQueryRouter


def profile():
    return {
        "profileId": "p1",
        "data": [
            {"dataId": "1", "dataType": "NOMBRE", "value": "DARVIN EVERARDO", "origins": [{"sourceCode": "REPUVE"}]},
            {"dataId": "2", "dataType": "APELLIDO PATERNO", "value": "LOPEZ", "origins": [{"sourceCode": "VRYR"}]},
            {"dataId": "3", "dataType": "APELLIDO MATERNO", "value": "BENITEZ"},
            {"dataId": "4", "dataType": "CURP", "value": "LOBD961202HJCPNR03", "origins": [{"sourceCode": "REPUVE"}]},
            {"dataId": "5", "dataType": "RFC", "value": "LOBD961202XXX"},
        ],
        "addresses": [
            {"addressId": "a1", "type": "DOMICILIO", "street": "AV TERCER MUNDO", "exteriorNumber": "178", "neighborhood": "CENTRO", "municipality": "SAN BLAS", "state": "NAYARIT", "postalCode": "63729", "origins": [{"sourceCode": "REPUVE"}]}
        ],
    }


def graph():
    return {
        "selectedNodeId": "p1",
        "nodes": [
            {"id": "p1", "type": "Persona", "title": "DARVIN EVERARDO LOPEZ BENITEZ"},
            {"id": "v1", "type": "Vehículo", "title": "JEEP 2000", "subtitle": "VIN123"},
            {"id": "v2", "type": "Vehículo", "title": "NISSAN", "subtitle": "VIN456"},
        ],
        "links": [
            {"id": "l1", "sourceId": "p1", "targetId": "v1"},
            {"id": "l2", "sourceId": "p1", "targetId": "v2"},
        ],
    }


def router():
    return FastQueryRouter(ContextBuilder(max_chars=8000, deep_max_chars=12000, cache_entries=8, cache_ttl_seconds=600))


def ask(question, thinking=False):
    return router().try_answer(profile=profile(), profile_id="p1", question=question, graph=graph(), selected_node=None, mode="auto", thinking=thinking)


def test_curp_is_direct():
    result = ask("¿Cuál es su CURP?")
    assert result is not None
    assert "LOBD961202HJCPNR03" in result.text


def test_vehicle_count_is_direct():
    result = ask("¿Cuántos vehículos tiene relacionados?")
    assert result is not None
    assert "2 vehículos" in result.text


def test_summary_is_direct_even_with_thinking_enabled():
    result = ask("dame un resumen del perfil", thinking=True)
    assert result is not None
    assert "DARVIN EVERARDO LOPEZ BENITEZ" in result.text
    assert "CURP" in result.text
    assert "2 vehículo" in result.text


def test_analytic_question_goes_to_llm():
    result = ask("analiza los patrones del grafo y propone hipótesis")
    assert result is None


def test_addresses_are_direct():
    result = ask("dame sus domicilios")
    assert result is not None
    assert "AV TERCER MUNDO 178" in result.text


def test_sources_are_direct():
    result = ask("qué fuentes tiene")
    assert result is not None
    assert "REPUVE" in result.text


def test_birth_date_can_be_direct_when_present():
    p = profile()
    p["data"].append({"dataId": "6", "dataType": "FECHA NACIMIENTO", "value": "02/12/1996"})
    result = router().try_answer(profile=p, profile_id="p1", question="cuál es su fecha de nacimiento", graph=graph(), selected_node=None, mode="auto", thinking=False)
    assert result is not None
    assert "02/12/1996" in result.text
