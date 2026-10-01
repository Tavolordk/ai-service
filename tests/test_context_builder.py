from app.context_builder import ContextBuilder, classify_intent


def sample_profile():
    return {
        "profileId": "p-1",
        "nombre": "DARVIN EVERARDO",
        "apellidoPaterno": "LOPEZ",
        "apellidoMaterno": "BENITEZ",
        "curp": "LOBD961202HJCPNR03",
        "rfc": "LOBD961202XXX",
        "addresses": [
            {
                "calle": "AV TERCER MUNDO",
                "numero": "178",
                "colonia": "CENTRO",
                "municipio": "SAN BLAS",
                "estado": "NAYARIT",
                "sources": ["REPUVE", "REPUVE", "VRYR"],
            }
        ],
        "vehicles": [
            {"marca": "JEEP", "modelo": "2000", "vin": f"VIN-{i:03d}", "source": "REPUVE"}
            for i in range(100)
        ],
    }


def test_intent_classifier():
    assert classify_intent("dame un resumen del perfil") == "summary"
    assert classify_intent("qué vehículos tiene") == "vehicles"
    assert classify_intent("cuáles son sus domicilios") == "addresses"


def test_context_is_compact_and_keeps_core_identity():
    builder = ContextBuilder(max_chars=5000, deep_max_chars=9000, max_facts_per_section=12)
    selection = builder.select(
        profile=sample_profile(),
        profile_id="p-1",
        question="dame un resumen del perfil",
        mode="auto",
    )
    assert len(selection.context) <= 5000
    assert "DARVIN EVERARDO" in selection.context
    assert "LOBD961202HJCPNR03" in selection.context
    assert selection.source_facts > selection.selected_facts
    assert selection.selected_facts < 100


def test_profile_snapshot_is_reused_when_payload_does_not_change():
    builder = ContextBuilder(max_chars=5000, cache_entries=4, cache_ttl_seconds=3600)
    first = builder.select(
        profile=sample_profile(), profile_id="p-1", question="dame un resumen", mode="auto"
    )
    second = builder.select(
        profile=sample_profile(), profile_id="p-1", question="qué vehículos tiene", mode="auto"
    )
    assert not first.cache_hit
    assert second.cache_hit
    stats = builder.cache_stats()
    assert stats["entries"] == 1
    assert stats["hits"] >= 1


def test_simple_identity_question_does_not_include_full_graph():
    builder = ContextBuilder(max_chars=5000)
    graph = {"nodes": [{"name": f"NODE-{i}", "vin": f"VIN-{i}"} for i in range(200)]}
    selection = builder.select(
        profile=sample_profile(),
        profile_id="p-1",
        question="cuál es su CURP",
        mode="auto",
        graph=graph,
    )
    assert "[GRAFO RELEVANTE]" not in selection.context
    assert "LOBD961202HJCPNR03" in selection.context
