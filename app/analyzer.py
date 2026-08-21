from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict

import networkx as nx

from .models import (
    Address,
    AnalysisResponse,
    AnswerResponse,
    Conflict,
    Datum,
    Evidence,
    GraphAnalysisResponse,
    GraphNodeInput,
    GraphPayload,
    Origin,
    ProfilePayload,
)


class ExactProfileAnalyzer:
    """Análisis determinista y narrativo: sólo cuenta lo que llegó en el payload/grafo."""

    def analyze(self, profile: ProfilePayload) -> AnalysisResponse:
        data = [item for item in (profile.data or []) if clean(item.value)]
        addresses = profile.addresses or []
        origins = collect_origins(profile)
        conflicts = detect_conflicts(data)
        facts = [datum_evidence(item) for item in data] + [
            address_evidence(address, index) for index, address in enumerate(addresses)
        ]
        metrics = {
            'populatedData': len(data),
            'uniqueFields': len(
                {normalize_code(item.code or item.dataType or item.dataId) for item in data}
            ),
            'addresses': len(addresses),
            'sources': len(origins),
            'conflicts': len(conflicts),
        }
        title = resolve_title(profile)
        summary = build_profile_story(profile, title, conflicts)

        recommendations: list[str] = []
        if conflicts:
            recommendations.append(
                f"Vale la pena contrastar {len(conflicts)} "
                f"{plural(len(conflicts), 'campo que trae valores distintos', 'campos que traen valores distintos')} "
                "con sus registros de origen."
            )
        if len(origins) > 1:
            recommendations.append(
                f"También puedes seguir los {len(origins)} registros de origen para ver qué aporta cada fuente y dónde coinciden."
            )
        if addresses:
            recommendations.append(
                f"Hay {len(addresses)} {plural(len(addresses), 'dirección para revisar', 'direcciones para revisar')} y ver qué fuentes respaldan cada una."
            )

        return AnalysisResponse(
            profileId=profile.profileId,
            title=title,
            summary=summary,
            metrics=metrics,
            facts=facts,
            conflicts=conflicts,
            recommendations=recommendations,
            guardrails=[
                'No se agregan hechos que no estén presentes en el payload o en el grafo recibido.',
                'Una diferencia entre valores no determina cuál valor es correcto.',
                'Un vínculo del grafo se describe como asociación visible; no se convierte en amistad, parentesco, delito, intención ni peligrosidad.',
            ],
        )

    def answer(
        self,
        question: str,
        profile: ProfilePayload,
        selected_node: GraphNodeInput | None = None,
        graph: GraphPayload | None = None,
    ) -> AnswerResponse:
        snapshot = self.analyze(profile)
        normalized_question = normalize_text(question)

        if not normalized_question:
            return self._story_answer(profile, snapshot, graph, selected_node)

        # Las preguntas específicas se resuelven ANTES del resumen general. Esto evita que
        # frases como "qué dirección aparece" terminen disparando "qué aparece" y devuelvan todo.
        if contains_any(normalized_question, ['direccion', 'direcciones', 'domicilio', 'domicilios']):
            return self._addresses_answer(
                profile,
                ordinal_index=extract_ordinal_index(normalized_question),
                include_sources=contains_any(
                    normalized_question,
                    ['fuente', 'fuentes', 'origen', 'origenes', 'de donde', 'reporta', 'reportan', 'proviene', 'sale'],
                ),
                count_only=bool(re.search(r'\b(cuant[oa]s?|numero|cantidad|cuenta)\b', normalized_question)),
            )

        field_answer = self._data_field_answer(profile, normalized_question)
        if field_answer is not None:
            return field_answer

        if contains_any(
            normalized_question,
            ['inconsistencia', 'inconsistencias', 'conflicto', 'conflictos', 'diferencia', 'diferencias', 'contradiccion', 'contradicciones'],
        ):
            return self._conflicts_answer(snapshot)

        if contains_any(
            normalized_question,
            ['vinculo', 'vinculos', 'relacion', 'relaciones', 'conexion', 'conexiones', 'conectado', 'conectada'],
        ):
            return self._relationships_answer(profile, snapshot, graph, selected_node)

        if contains_any(normalized_question, ['nodo', 'seleccionado', 'seleccionada', 'activo', 'activa']) and selected_node:
            return self._node_answer(selected_node, graph)

        if contains_any(normalized_question, ['fuente', 'fuentes', 'origen', 'origenes', 'reporta', 'reportan', 'proviene', 'sale']):
            return self._sources_answer(profile, normalized_question)

        if contains_any(normalized_question, ['recomendacion', 'recomendaciones', 'revisar', 'siguiente', 'siguientes']):
            evidence = [item for conflict in snapshot.conflicts for item in conflict.evidence][:12]
            return AnswerResponse(
                text=(
                    f"Si quieres seguirle el hilo, yo revisaría esto: {' '.join(snapshot.recommendations)}"
                    if snapshot.recommendations
                    else 'Con lo que está visible no encuentro una revisión adicional concreta que sugerir.'
                ),
                evidence=evidence,
                disclaimer='Las sugerencias sólo señalan qué información conviene revisar; no califican a la persona.',
            )

        if contains_any(
            normalized_question,
            ['chisme', 'cuentame', 'todo sobre', 'todo lo que', 'persona', 'quien es', 'que sabes', 'que aparece'],
        ):
            return self._story_answer(profile, snapshot, graph, selected_node)

        if contains_any(
            normalized_question,
            ['dato', 'datos', 'campo', 'campos', 'informacion', 'perfil', 'resume', 'resumen', 'describe', 'descripcion'],
        ):
            return self._story_answer(profile, snapshot, graph, selected_node)

        return AnswerResponse(
            text='Eso no aparece de forma identificable en los datos que recibí. Puedo responderte con precisión sobre cualquier campo, dirección, fuente o vínculo que sí venga en el perfil o en el grafo.',
            evidence=[],
            disclaimer='No completo respuestas con información externa ni invento datos ausentes.',
        )

    @staticmethod
    def _story_answer(
        profile: ProfilePayload,
        snapshot: AnalysisResponse,
        graph: GraphPayload | None,
        selected_node: GraphNodeInput | None,
    ) -> AnswerResponse:
        title = snapshot.title or 'esta persona'
        paragraphs: list[str] = [
            f"Mira, de {title} hay bastante información para seguirle el hilo. {build_personal_details_sentence(profile)} "
            'Te lo cuento tal como viene en las fuentes, sin completar ni corregir datos por mi cuenta.'
        ]

        addresses = profile.addresses or []
        if addresses:
            places = [compact_address_place(address) for address in addresses[:4]]
            places = [place for place in places if place]
            if len(addresses) == 1:
                place_text = f", ubicado en {places[0]}" if places else ''
                paragraphs.append(
                    f"En domicilios aparece uno registrado{place_text}. Si quieres, te doy la dirección completa y quién la reporta."
                )
            else:
                place_text = f" Se reparten entre {natural_join(places)}." if places else ''
                paragraphs.append(
                    f"En domicilios aparecen {len(addresses)}.{place_text} "
                    'No te aviento de golpe todas las calles y números: puedes pedirme la primera, la segunda, la última '
                    'o preguntar qué fuente reporta cada una.'
                )
        else:
            paragraphs.append('En domicilios, por ahora no me llegó ninguno registrado para esta persona.')

        relationship_text, node_evidence = graph_story(profile, graph, selected_node)
        if relationship_text:
            paragraphs.append(relationship_text)
        else:
            origins = collect_origins(profile)
            if origins:
                grouped = grouped_origin_labels(origins)
                paragraphs.append(
                    f"La información está respaldada por {len(origins)} registros de origen. "
                    f"Entre las fuentes que más aparecen están {format_counter(grouped, limit=5)}."
                )

        if snapshot.conflicts:
            conflict_names = ', '.join(conflict.field for conflict in snapshot.conflicts[:6])
            paragraphs.append(
                f"Y aquí sí hay algo que vale la pena mirar con calma: encontré {len(snapshot.conflicts)} "
                f"{plural(len(snapshot.conflicts), 'campo con valores diferentes', 'campos con valores diferentes')}. "
                f"Aparecen en {conflict_names}. Eso sólo significa que las fuentes no muestran exactamente lo mismo; no decide cuál dato es el correcto."
            )
        else:
            paragraphs.append(
                'Y por ahora las fuentes no se están contradiciendo en los campos que pude comparar. Eso está bien para seguir el análisis, aunque no significa que yo haya validado esos datos contra una fuente externa.'
            )

        evidence = (snapshot.facts[:8] + node_evidence[:4])[:12]
        return AnswerResponse(
            text='\n\n'.join(part for part in paragraphs if part.strip()),
            evidence=evidence,
            disclaimer='Te lo cuento en tono conversacional, pero cada afirmación sale únicamente del payload o del grafo recibido. “Aparece relacionado” no implica por sí solo amistad, parentesco, relación personal o relación delictiva.',
        )

    @staticmethod
    def _relationships_answer(
        profile: ProfilePayload,
        snapshot: AnalysisResponse,
        graph: GraphPayload | None,
        selected_node: GraphNodeInput | None,
    ) -> AnswerResponse:
        title = snapshot.title or 'esta persona'
        relationship_text, evidence = graph_story(profile, graph, selected_node, relationship_only=True)
        if relationship_text:
            return AnswerResponse(
                text=relationship_text,
                evidence=evidence[:12],
                disclaimer='Sólo describo conexiones que existen como aristas en el grafo recibido; no deduzco el tipo de relación más allá de lo que el nodo indica.',
            )

        origins = collect_origins(profile)
        if not origins:
            return AnswerResponse(
                text=f"De {title}, el payload que recibí no trae vínculos de grafo ni registros de origen que pueda describir como conexiones.",
                evidence=[],
                disclaimer='No agrego relaciones que no estén presentes en los datos recibidos.',
            )
        grouped = grouped_origin_labels(origins)
        return AnswerResponse(
            text=(
                f"Sobre los vínculos de {title}, todavía no tengo un grafo de entidades en esta petición, pero sí veo "
                f"{len(origins)} registros de origen asociados a sus datos. Se reparten principalmente entre {format_counter(grouped, limit=6)}."
            ),
            evidence=[origin_evidence(origin, index) for index, origin in enumerate(origins[:12])],
            disclaimer='Los registros de origen indican de dónde salió la información; no equivalen por sí solos a vínculos personales.',
        )

    @staticmethod
    def _conflicts_answer(snapshot: AnalysisResponse) -> AnswerResponse:
        if not snapshot.conflicts:
            return AnswerResponse(
                text=(
                    f"Por ahora no encuentro datos que se estén contradiciendo dentro del perfil de {snapshot.title or 'esta persona'}. "
                    'Cuando un mismo campo aparece varias veces, los valores recibidos coinciden entre sí.'
                ),
                evidence=snapshot.facts[:8],
                disclaimer='Esto no valida la información contra fuentes externas; sólo compara los valores presentes en el payload recibido.',
            )
        descriptions = [
            f"{conflict.field} aparece como {' / '.join(conflict.values)}" for conflict in snapshot.conflicts
        ]
        return AnswerResponse(
            text=(
                f"Aquí sí hay datos que no cuentan exactamente la misma historia sobre {snapshot.title or 'esta persona'}: "
                + '; '.join(descriptions)
                + '. Yo los tomaría como puntos para contrastar con sus fuentes de origen.'
            ),
            evidence=[item for conflict in snapshot.conflicts for item in conflict.evidence],
            disclaimer='Una diferencia de valores no implica que alguno sea incorrecto; sólo indica que el payload contiene más de un valor para el mismo campo.',
        )

    @staticmethod
    def _addresses_answer(
        profile: ProfilePayload,
        ordinal_index: int | None = None,
        include_sources: bool = False,
        count_only: bool = False,
    ) -> AnswerResponse:
        addresses = profile.addresses or []
        title = resolve_title(profile) or 'esta persona'
        if not addresses:
            return AnswerResponse(
                text=f"De {title}, en lo que recibí no aparece ninguna dirección registrada.",
                evidence=[],
                disclaimer='No se infieren domicilios que no estén incluidos en el payload.',
            )

        if count_only:
            return AnswerResponse(
                text=f"De {title} aparecen {len(addresses)} {plural(len(addresses), 'dirección registrada', 'direcciones registradas')}.",
                evidence=[address_evidence(address, index) for index, address in enumerate(addresses[:3])],
                disclaimer='El conteo se obtiene únicamente de las direcciones presentes en el payload recibido.',
            )

        if ordinal_index is not None:
            resolved_index = len(addresses) - 1 if ordinal_index == -1 else ordinal_index
            if resolved_index < 0 or resolved_index >= len(addresses):
                requested = 'última' if ordinal_index == -1 else f'#{ordinal_index + 1}'
                return AnswerResponse(
                    text=f"No existe esa dirección en lo que recibí: pediste la {requested}, pero el perfil sólo trae {len(addresses)}.",
                    evidence=[],
                    disclaimer='No sustituyo una posición inexistente por otra dirección.',
                )
            item = address_evidence(addresses[resolved_index], resolved_index)
            position = 'última' if ordinal_index == -1 else ordinal_label(resolved_index)
            source_text = ''
            if include_sources:
                source_text = (
                    f" La reportan las fuentes {natural_join(item.sourceCodes)}."
                    if item.sourceCodes
                    else ' Esa dirección no trae código de fuente visible en el payload.'
                )
            return AnswerResponse(
                text=f"La {position} dirección de {title} es: {item.value}.{source_text}",
                evidence=[item],
                disclaimer='Se devuelve únicamente la posición solicitada, respetando el orden recibido en el payload.',
            )

        evidence = [address_evidence(address, index) for index, address in enumerate(addresses)]
        descriptions = [f'{index + 1}) {item.value}' for index, item in enumerate(evidence)]
        return AnswerResponse(
            text=(
                f"En la parte de domicilios de {title} aparecen {len(addresses)} "
                f"{plural(len(addresses), 'dirección', 'direcciones')}. "
                + ' '.join(descriptions)
                + ' Si una dirección trae varias fuentes, eso significa únicamente que más de un registro la reporta.'
            ),
            evidence=evidence,
            disclaimer='Las direcciones se describen tal como fueron recibidas; no se geocodifican ni completan datos faltantes.',
        )

    @staticmethod
    def _data_field_answer(profile: ProfilePayload, normalized_question: str) -> AnswerResponse | None:
        requested = requested_field_aliases(normalized_question)
        if not requested:
            return None

        matched: list[Datum] = []
        requested_variants = {
            normalize_text(variant)
            for canonical in requested
            for variant in (canonical, *FIELD_QUERY_ALIASES.get(canonical, ()))
        }
        for item in profile.data or []:
            label = normalize_text(humanize(item.code or item.dataType or item.dataId))
            code = normalize_text(item.code or item.dataType or '')
            searchable_compact = compact_normalized(f'{label} {code}')
            if any(
                variant in label
                or variant in code
                or compact_normalized(variant) in searchable_compact
                for variant in requested_variants
            ):
                if clean(item.value):
                    matched.append(item)

        title = resolve_title(profile) or 'esta persona'
        if not matched:
            fields = natural_join(list(requested))
            return AnswerResponse(
                text=f"De {title}, el campo {fields} no aparece con valor en los datos que recibí.",
                evidence=[],
                disclaimer='No completo campos faltantes con información externa.',
            )

        evidence = [datum_evidence(item) for item in matched]
        parts = [f"{item.label}: {item.value}" for item in evidence]
        source_parts = []
        for item in evidence:
            if item.sourceCodes:
                source_parts.append(f"{item.label} viene de {natural_join(item.sourceCodes)}")
        source_text = f" Fuentes: {'; '.join(source_parts)}." if contains_any(normalized_question, ['fuente', 'fuentes', 'origen', 'origenes', 'de donde', 'reporta', 'reportan']) and source_parts else ''
        return AnswerResponse(
            text=f"De {title}, {'; '.join(parts)}.{source_text}",
            evidence=evidence,
            disclaimer='La respuesta usa únicamente los valores del campo solicitado y sus orígenes recibidos.',
        )

    @staticmethod
    def _sources_answer(profile: ProfilePayload, normalized_question: str = '') -> AnswerResponse:
        origins = collect_origins(profile)
        title = resolve_title(profile) or 'esta persona'
        if not origins:
            return AnswerResponse(
                text=f"De {title} no recibí registros de origen asociados a sus datos o direcciones.",
                evidence=[],
                disclaimer='No se agregan fuentes que no estén presentes en el payload.',
            )

        # Si la pregunta nombra una fuente concreta, respondemos qué datos respalda ESA fuente
        # en vez de devolver el catálogo completo de orígenes.
        available_codes = sorted({clean(origin.sourceCode) for origin in origins if clean(origin.sourceCode)})
        question_compact = compact_normalized(normalized_question)
        requested_codes = [
            code for code in available_codes
            if normalize_text(code) in normalized_question or compact_normalized(code) in question_compact
        ]
        if requested_codes:
            requested_normalized = {normalize_text(code) for code in requested_codes}
            evidence: list[Evidence] = []
            for datum in profile.data or []:
                item = datum_evidence(datum)
                if clean(item.value) and requested_normalized & {normalize_text(code) for code in item.sourceCodes}:
                    evidence.append(item)
            for index, address in enumerate(profile.addresses or []):
                item = address_evidence(address, index)
                if requested_normalized & {normalize_text(code) for code in item.sourceCodes}:
                    evidence.append(item)

            if evidence:
                details = '; '.join(f'{item.label}: {item.value}' for item in evidence)
                return AnswerResponse(
                    text=f"De {title}, la fuente {natural_join(requested_codes)} respalda esto en los datos recibidos: {details}.",
                    evidence=evidence,
                    disclaimer='Se muestran únicamente datos y direcciones cuyo origin.sourceCode coincide con la fuente solicitada.',
                )
            return AnswerResponse(
                text=f"La fuente {natural_join(requested_codes)} aparece en los orígenes recibidos, pero no encontré un dato visible asociado que pueda describir.",
                evidence=[
                    origin_evidence(origin, index)
                    for index, origin in enumerate(origins)
                    if normalize_text(clean(origin.sourceCode)) in requested_normalized
                ],
                disclaimer='No se atribuyen datos a una fuente si esa asociación no viene en origins.',
            )

        evidence = [origin_evidence(origin, index) for index, origin in enumerate(origins)]
        grouped = grouped_origin_labels(origins)
        return AnswerResponse(
            text=(
                f"Lo que aparece de {title} viene de {len(origins)} registros de origen. "
                f"Las fuentes que más se repiten son {format_counter(grouped, limit=8)}. "
                'Eso te sirve para ubicar rápidamente de dónde se está formando el perfil y qué fuente aporta más registros visibles.'
            ),
            evidence=evidence,
            disclaimer='La lista se obtiene exclusivamente de los objetos origins recibidos en datos y direcciones.',
        )

    @staticmethod
    def _node_answer(node: GraphNodeInput, graph: GraphPayload | None) -> AnswerResponse:
        visible_details: list[str] = []
        for detail in node.details:
            label = clean(str(detail.get('label', 'Dato')))
            value = clean(str(detail.get('value', '')))
            if value and value != '—':
                visible_details.append(f'{label}: {value}')

        connected_nodes = connected_graph_nodes(graph, node.id) if graph else []
        if connected_nodes:
            connection_text = (
                f" En el grafo está conectado directamente con {len(connected_nodes)} "
                f"{plural(len(connected_nodes), 'nodo', 'nodos')}: {format_node_titles(connected_nodes, 6)}."
            )
        else:
            connection_text = ' No veo conexiones directas adicionales para este nodo dentro del grafo recibido.'

        detail_text = '; '.join(visible_details) if visible_details else 'sin detalle adicional visible'
        return AnswerResponse(
            text=(
                f"Mira, el nodo que tienes seleccionado es {node.title}. "
                f"En pantalla aparece como {node.type.lower()}. "
                + (f"Su referencia visible es {node.subtitle}. " if clean(node.subtitle) else '')
                + f"Lo que trae como detalle es: {detail_text}.{connection_text}"
            ),
            evidence=[node_to_evidence(node)] + [node_to_evidence(item) for item in connected_nodes[:6]],
            disclaimer='La descripción corresponde únicamente al nodo seleccionado y a las aristas presentes en el grafo recibido.',
        )


class ExactGraphAnalyzer:
    """Calcula y cuenta únicamente relaciones presentes en nodos/aristas recibidos."""

    def analyze(self, payload: GraphPayload) -> GraphAnalysisResponse:
        graph = nx.Graph()
        node_map = {node.id: node for node in payload.nodes}
        graph.add_nodes_from(node_map.keys())
        graph.add_edges_from((link.sourceId, link.targetId) for link in payload.links)
        components = nx.number_connected_components(graph) if graph.number_of_nodes() else 0
        selected = payload.selectedNodeId if payload.selectedNodeId in graph else None
        connected_ids = list(graph.neighbors(selected)) if selected else []
        connected_nodes = [node_map[node_id] for node_id in connected_ids if node_id in node_map]
        degree = int(graph.degree(selected)) if selected else 0

        if selected and selected in node_map:
            selected_node = node_map[selected]
            description = (
                f"Sobre {selected_node.title}, lo que se ve en este grafo es que tiene {degree} "
                f"{plural(degree, 'conexión directa', 'conexiones directas')}."
            )
            if connected_nodes:
                description += f" Sus vínculos visibles llevan a {format_node_titles(connected_nodes, 7)}."
                type_counter = Counter(human_node_type(node.type) for node in connected_nodes)
                description += f" Por tipo, esas conexiones se reparten en {format_counter(type_counter, limit=6)}."
        else:
            description = (
                f"A simple vista, el grafo trae {graph.number_of_nodes()} nodos y {graph.number_of_edges()} vínculos. "
                f"Se forman {components} {plural(components, 'grupo conectado', 'grupos conectados')}; sólo estoy contando las relaciones recibidas, sin agregar ninguna."
            )

        return GraphAnalysisResponse(
            nodeCount=graph.number_of_nodes(),
            linkCount=graph.number_of_edges(),
            componentCount=components,
            selectedNodeId=selected,
            selectedDegree=degree,
            connectedNodeIds=sorted(connected_ids),
            description=description,
        )


def build_profile_story(profile: ProfilePayload, title: str, conflicts: list[Conflict]) -> str:
    subject = title or 'esta persona'
    details = build_personal_details_sentence(profile)
    addresses = profile.addresses or []
    origins = collect_origins(profile)
    conflict_phrase = (
        f"Sí hay {len(conflicts)} {plural(len(conflicts), 'dato que conviene contrastar', 'datos que conviene contrastar')} porque aparecen con valores distintos."
        if conflicts
        else 'Dentro de lo recibido, los campos repetidos no muestran valores diferentes entre sí.'
    )
    places = [compact_address_place(address) for address in addresses[:4]]
    places = [place for place in places if place]
    place_text = f" Los domicilios se mueven entre {natural_join(places)}." if places else ''
    return (
        f"Mira, de {subject} hay bastante información para seguirle el hilo. {details} "
        f"También aparecen {len(addresses)} {plural(len(addresses), 'domicilio registrado', 'domicilios registrados')} "
        f"respaldados dentro de {len(origins)} {plural(len(origins), 'registro de origen', 'registros de origen')}.{place_text} "
        f"{conflict_phrase}"
    )


def build_personal_details_sentence(profile: ProfilePayload) -> str:
    preferred = [
        ({'FECHANACIMIENTO', 'BIRTHDATE', 'DATEOFBIRTH'}, 'fecha de nacimiento'),
        ({'SEXO', 'SEX', 'GENDER'}, 'sexo'),
        ({'ESTADOCIVIL', 'MARITALSTATUS'}, 'estado civil'),
        ({'NACIONALIDAD', 'NATIONALITY'}, 'nacionalidad'),
        ({'EMPLOYMENTDEPENDENCE', 'DEPENDENCIA', 'DEPENDENCIAEMPLEO'}, 'dependencia laboral'),
        ({'EMPLOYMENTCORPORATION', 'CORPORACION', 'CORPORACIONEMPLEO'}, 'corporación laboral'),
        ({'OCUPACION', 'OCCUPATION', 'CARGO'}, 'ocupación/cargo'),
    ]
    values: list[str] = []
    for codes, label in preferred:
        value = find_value(profile, codes)
        if value:
            values.append(f'{label}: {value}')
    if not values:
        populated = [item for item in (profile.data or []) if clean(item.value)]
        return (
            f"El perfil trae {len(populated)} {plural(len(populated), 'dato con valor', 'datos con valor')}."
            if populated
            else 'El perfil no trae datos personales con valor para describir.'
        )
    return 'Entre sus datos personales aparecen ' + natural_join(values[:6]) + '.'


def graph_story(
    profile: ProfilePayload,
    graph_payload: GraphPayload | None,
    selected_node: GraphNodeInput | None,
    relationship_only: bool = False,
) -> tuple[str, list[Evidence]]:
    if not graph_payload or not graph_payload.nodes:
        return '', []

    node_map = {node.id: node for node in graph_payload.nodes}
    root = node_map.get(profile.profileId)
    if root is None:
        root = next(
            (
                node for node in graph_payload.nodes
                if normalize_text(node.type) in {'perfil', 'persona', 'person', 'profile'}
            ),
            None,
        )
    if root is None:
        return '', []

    connected = connected_graph_nodes(graph_payload, root.id)
    title = resolve_title(profile) or root.title or 'esta persona'
    if not connected:
        return (
            f"En el grafo, {title} aparece como nodo principal pero no tiene conexiones directas en las aristas que recibí.",
            [node_to_evidence(root)],
        )

    type_counter = Counter(human_node_type(node.type) for node in connected)
    title_counter = Counter(clean(node.title) or human_node_type(node.type) for node in connected)
    story = (
        f"Y en el grafo sí hay bastante movimiento alrededor de {title}: tiene {len(connected)} "
        f"{plural(len(connected), 'conexión directa', 'conexiones directas')}. "
        f"Lo que más se repite entre esos vínculos es {format_counter(title_counter, limit=5)}."
    )

    selected = selected_node or (node_map.get(graph_payload.selectedNodeId) if graph_payload.selectedNodeId else None)
    if selected and selected.id != root.id:
        selected_neighbors = connected_graph_nodes(graph_payload, selected.id)
        direct_to_root = any(node.id == root.id for node in selected_neighbors)
        if direct_to_root:
            story += (
                f" Ahora mismo tienes seleccionado {selected.title}: ese nodo sí tiene una conexión directa con {title} dentro del grafo."
            )
        elif selected_neighbors:
            story += (
                f" Ahora mismo tienes seleccionado {selected.title}. No está unido directamente a {title} en las aristas recibidas; "
                f"sus conexiones directas visibles son {format_node_titles(selected_neighbors, 5)}."
            )

    if not relationship_only:
        story += ' Aquí “conexión” significa únicamente que el grafo los enlaza; no estoy suponiendo una relación personal que los datos no indiquen.'

    evidence = [node_to_evidence(root)] + [node_to_evidence(node) for node in connected[:11]]
    return story, evidence


def connected_graph_nodes(payload: GraphPayload | None, node_id: str) -> list[GraphNodeInput]:
    if not payload:
        return []
    node_map = {node.id: node for node in payload.nodes}
    connected_ids: list[str] = []
    for link in payload.links:
        if link.sourceId == node_id:
            connected_ids.append(link.targetId)
        elif link.targetId == node_id:
            connected_ids.append(link.sourceId)
    result: list[GraphNodeInput] = []
    seen: set[str] = set()
    for connected_id in connected_ids:
        if connected_id in seen or connected_id not in node_map:
            continue
        seen.add(connected_id)
        result.append(node_map[connected_id])
    return result


def node_to_evidence(node: GraphNodeInput) -> Evidence:
    detail_values = []
    for detail in node.details:
        label = clean(str(detail.get('label', '')))
        value = clean(str(detail.get('value', '')))
        if label and value and value != '—':
            detail_values.append(f'{label}: {value}')
    return Evidence(
        kind='node',
        id=node.id,
        label=node.title,
        value='; '.join(detail_values[:4]) or node.subtitle or human_node_type(node.type),
        sourceCodes=[],
    )


def human_node_type(value: str) -> str:
    normalized = normalize_text(value)
    mapping = {
        'person': 'persona',
        'persona': 'persona',
        'profile': 'perfil',
        'perfil': 'perfil',
        'source': 'fuente',
        'fuente': 'fuente',
        'vehicle': 'vehículo',
        'vehiculo': 'vehículo',
        'address': 'dirección',
        'direccion': 'dirección',
        'organization': 'organización',
        'organizacion': 'organización',
        'phone': 'teléfono',
        'telefono': 'teléfono',
    }
    return mapping.get(normalized, clean(value).lower() or 'entidad')


def grouped_origin_labels(origins: list[Origin]) -> Counter[str]:
    return Counter(clean(origin.sourceCode) or 'Fuente sin código' for origin in origins)


def format_counter(counter: Counter[str], limit: int = 6) -> str:
    if not counter:
        return 'sin categorías disponibles'
    ordered = counter.most_common(limit)
    values = [f'{label} ({count})' for label, count in ordered]
    remaining = sum(counter.values()) - sum(count for _, count in ordered)
    if remaining > 0:
        values.append(f'{remaining} más')
    return natural_join(values)


def format_node_titles(nodes: list[GraphNodeInput], limit: int = 6) -> str:
    titles = [clean(node.title) or human_node_type(node.type) for node in nodes[:limit]]
    if len(nodes) > limit:
        titles.append(f'{len(nodes) - limit} más')
    return natural_join(titles)


def natural_join(values: list[str]) -> str:
    values = [value for value in values if value]
    if not values:
        return ''
    if len(values) == 1:
        return values[0]
    return ', '.join(values[:-1]) + ' y ' + values[-1]


def detect_conflicts(data: list[Datum]) -> list[Conflict]:
    groups: dict[str, list[Datum]] = defaultdict(list)
    for item in data:
        groups[normalize_code(item.code or item.dataType or item.dataId)].append(item)
    result: list[Conflict] = []
    for items in groups.values():
        distinct: dict[str, str] = {}
        for item in items:
            value = clean(item.value)
            if value:
                distinct.setdefault(normalize_text(value), value)
        if len(distinct) > 1:
            first = items[0]
            result.append(
                Conflict(
                    field=humanize(first.code or first.dataType or first.dataId),
                    values=list(distinct.values()),
                    evidence=[datum_evidence(item) for item in items],
                )
            )
    return result


def datum_evidence(item: Datum) -> Evidence:
    return Evidence(
        kind='data',
        id=item.dataId,
        label=humanize(item.code or item.dataType or 'Dato'),
        value=clean(item.value) or '—',
        sourceCodes=unique_source_codes(item.origins or []),
    )


def address_evidence(address: Address, index: int) -> Evidence:
    return Evidence(
        kind='address',
        id=address.addressId,
        label=clean(address.type) or f'Dirección {index + 1}',
        value=format_address(address),
        sourceCodes=unique_source_codes(address.origins or []),
    )


def origin_evidence(origin: Origin, index: int) -> Evidence:
    source = clean(origin.sourceCode) or f'Fuente {index + 1}'
    record = clean(origin.sourceRecordId)
    return Evidence(
        kind='origin',
        id=clean(origin.originId) or f'origin-{index}',
        label='Origen',
        value=f'{source} ({record})' if record else source,
        sourceCodes=[source] if clean(origin.sourceCode) else [],
    )


def collect_origins(profile: ProfilePayload) -> list[Origin]:
    seen: set[str] = set()
    result: list[Origin] = []
    for item in profile.data or []:
        for origin in item.origins or []:
            key = '|'.join(
                filter(None, [clean(origin.originId), clean(origin.sourceCode), clean(origin.sourceRecordId)])
            )
            if key and key not in seen:
                seen.add(key)
                result.append(origin)
    for item in profile.addresses or []:
        for origin in item.origins or []:
            key = '|'.join(
                filter(None, [clean(origin.originId), clean(origin.sourceCode), clean(origin.sourceRecordId)])
            )
            if key and key not in seen:
                seen.add(key)
                result.append(origin)
    return result


def unique_source_codes(origins: list[Origin]) -> list[str]:
    return list(dict.fromkeys(code for code in (clean(origin.sourceCode) for origin in origins) if code))


def resolve_title(profile: ProfilePayload) -> str:
    direct = find_value(profile, {'NOMBRECOMPLETO', 'FULLNAME'})
    if direct:
        return direct
    return ' '.join(
        filter(
            None,
            [
                find_value(profile, {'NOMBRE', 'NOMBRES', 'NAME', 'FIRSTNAME'}),
                find_value(profile, {'APELLIDOPATERNO', 'PRIMERAPELLIDO', 'LASTNAME', 'SURNAME'}),
                find_value(profile, {'APELLIDOMATERNO', 'SEGUNDOAPELLIDO', 'SECONDLASTNAME', 'MOTHERSLASTNAME'}),
            ],
        )
    ).strip()


def find_value(profile: ProfilePayload, codes: set[str]) -> str:
    wanted = {normalize_code(code) for code in codes}
    for item in profile.data or []:
        if normalize_code(item.code or '') in wanted:
            return clean(item.value)
    return ''


def compact_address_place(address: Address) -> str:
    municipality = clean(address.municipality)
    state = clean(address.state)
    if municipality and state:
        return f'{municipality}, {state}'
    return municipality or state


def format_address(address: Address) -> str:
    street = ' '.join(filter(None, [clean(address.street), clean(address.exteriorNumber)]))
    interior = f'Int. {clean(address.interiorNumber)}' if clean(address.interiorNumber) else ''
    postal = f'C.P. {clean(address.postalCode)}' if clean(address.postalCode) else ''
    return ', '.join(
        filter(
            None,
            [street, interior, clean(address.neighborhood), clean(address.municipality), clean(address.state), postal],
        )
    ) or 'dirección consolidada sin detalle adicional'


FIELD_QUERY_ALIASES: dict[str, tuple[str, ...]] = {
    'curp': ('curp',),
    'rfc': ('rfc',),
    'cuip': ('cuip',),
    'cib': ('cib',),
    'nombre': ('nombre', 'nombres', 'nombre completo'),
    'apellido paterno': ('apellido paterno', 'primer apellido'),
    'apellido materno': ('apellido materno', 'segundo apellido'),
    'fecha de nacimiento': ('fecha nacimiento', 'fecha de nacimiento', 'nacimiento'),
    'sexo': ('sexo',),
    'estado civil': ('estado civil',),
    'nacionalidad': ('nacionalidad',),
    'dependencia': ('dependencia', 'dependencia laboral', 'employment dependence'),
    'corporacion': ('corporacion', 'corporacion laboral', 'employment corporation'),
    'ocupacion': ('ocupacion', 'cargo', 'ocupacion cargo'),
}


def requested_field_aliases(normalized_question: str) -> tuple[str, ...]:
    result: list[str] = []
    for canonical, aliases in FIELD_QUERY_ALIASES.items():
        if any(alias in normalized_question for alias in aliases):
            result.append(canonical)
    return tuple(result)


def extract_ordinal_index(value: str) -> int | None:
    normalized = normalize_text(value)
    patterns: list[tuple[str, int]] = [
        (r'\b(primera|primer|primero|1ra|1er|1ero|1)\b', 0),
        (r'\b(segunda|segundo|2da|2do|2)\b', 1),
        (r'\b(tercera|tercer|tercero|3ra|3er|3ero|3)\b', 2),
        (r'\b(cuarta|cuarto|4ta|4to|4)\b', 3),
        (r'\b(quinta|quinto|5ta|5to|5)\b', 4),
        (r'\b(sexta|sexto|6ta|6to|6)\b', 5),
        (r'\b(septima|septimo|7ma|7mo|7)\b', 6),
        (r'\b(octava|octavo|8va|8vo|8)\b', 7),
        (r'\b(novena|noveno|9na|9no|9)\b', 8),
        (r'\b(decima|decimo|10ma|10mo|10)\b', 9),
    ]
    if re.search(r'\b(ultima|ultimo)\b', normalized):
        return -1
    for pattern, index in patterns:
        if re.search(pattern, normalized):
            return index
    return None


def ordinal_label(index: int) -> str:
    labels = ['primera', 'segunda', 'tercera', 'cuarta', 'quinta', 'sexta', 'séptima', 'octava', 'novena', 'décima']
    return labels[index] if 0 <= index < len(labels) else f'dirección #{index + 1}'


def natural_join(values: list[str]) -> str:
    clean_values = [clean(value) for value in values if clean(value)]
    if not clean_values:
        return ''
    if len(clean_values) == 1:
        return clean_values[0]
    return ', '.join(clean_values[:-1]) + ' y ' + clean_values[-1]


def clean(value: str | None) -> str:
    return (value or '').strip()


def normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize('NFD', value)
    return ''.join(character for character in decomposed if unicodedata.category(character) != 'Mn').lower().strip()


def compact_normalized(value: str) -> str:
    return re.sub(r'[^a-z0-9]', '', normalize_text(value))


def normalize_code(value: str) -> str:
    return re.sub(r'[^a-z0-9]', '', normalize_text(value)).upper()


def humanize(value: str) -> str:
    text = re.sub(r'[_-]+', ' ', clean(value))
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    return text.lower().capitalize() or 'Dato'


def contains_any(text: str, values: list[str]) -> bool:
    return any(value in text for value in values)


def plural(number: int, singular: str, plural_value: str) -> str:
    return singular if number == 1 else plural_value
