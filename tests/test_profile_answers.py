"""Pruebas sin modelos ni datos reales. Ejecutar: python -m pytest -q tests/test_profile_answers.py"""
from datetime import date

from app.profile_answers import answer_profile_question


def sample_profile():
    fields = {
        'NOMBRE': 'JUANA MARIA', 'APELLIDO_PATERNO':'PEREZ', 'APELLIDO_MATERNO':'GARCIA',
        'FECHA_NACIMIENTO':'1983-11-11', 'SEXO':'FEMENINO', 'ESTADO_CIVIL':'CASADO (A)',
        'NACIONALIDAD':'MEXICANA', 'CURP':'PEGJ831111MDFZZZ00', 'CUIP':'FICTICIO0001',
        'RFC':'PEGJ831111AAA', 'CIB':'CIB-FICTICIO',
        'EMPLOYMENT_DEPENDENCIA':'SEGURIDAD PUBLICA MUNICIPAL',
        'EMPLOYMENT_CORPORACION':'DIRECCION MUNICIPAL',
        'EMPLOYMENT_AREA':'OPERATIVA', 'EMPLOYMENT_ENTIDAD_ADSCRIPCION':'SONORA',
        'EMPLOYMENT_MUNICIPIO_ADSCRIPCION':'HERMOSILLO',
        'EMPLOYMENT_PUESTO':'POLICIA PREVENTIVO',
        'EMPLOYMENT_NIVEL_MANDO':'MANDO INFERIOR',
        'EMPLOYMENT_ESTATUS_PERSONA':'ACTIVO',
        'EMPLOYMENT_ESTATUS_ADSCRIPCION':'ACTIVO',
    }
    return {'profileId':'ficticio', 'data':[
        {'dataId':f'ficticio-{i}', 'code':code, 'dataType':'Attribute', 'value':value,
         'origins':[{'sourceCode':'RNPSP','sourceRecordId':'registro-ficticio'}]}
        for i,(code,value) in enumerate(fields.items())]}


def answer(q, today=date(2026,10,13), profile=None):
    return answer_profile_question(profile or sample_profile(), q, today=today)


def test_edad_antes_y_despues_de_cumpleanos():
    assert '42 años' in answer('¿Cuál es su edad?').text
    assert '43 años' in answer('¿Cuántos años tiene?',date(2026,11,11)).text
    assert '41 años' in answer('¿Qué edad tiene?',date(2025,11,10)).text
    assert len(answer('¿Cuál es su edad?').evidence) == 1


def test_colores_y_datos_fisicos_no_inventados():
    for question in ('¿Cuál es su color de piel?', '¿De qué color tiene los ojos?',
                     '¿Cuál es su peso?', '¿Cuál es su estatura?'):
        result=answer(question)
        # La ausencia debe ser explícita, nunca un resultado ficticio.
        assert result is not None and not result.evidence and 'No hay información' in result.text


def test_datos_personales():
    for question,expected in (
        ('¿Cuál es su nombre?','JUANA MARIA'),
        ('¿Cuál es su apellido paterno?','PEREZ'),
        ('¿Cuál es su fecha de nacimiento?','11/11/1983'),
        ('¿En qué año nació?','1983'),
        ('¿Cuál es su sexo?','FEMENINO'),
        ('¿Cuál es su estado civil?','CASADO (A)'),
        ('¿Cuál es su nacionalidad?','MEXICANA'),
        ('Dime el RFC','PEGJ831111AAA'),
        ('¿Cuál es su CIB?','CIB-FICTICIO'),
    ):
        result=answer(question)
        assert result is not None and expected in result.text, question
        assert '$.profile' not in result.text
        assert result.evidence


def test_no_confundir_nacionalidad_con_nacimiento():
    result=answer('¿Cuál es su nacionalidad?')
    assert 'MEXICANA' in result.text
    assert 'Nacimiento:' not in result.text
    assert len(result.evidence)==1


def test_puesto_y_corporacion():
    assert 'POLICIA PREVENTIVO' in answer('¿Cuál es su empleo?').text
    assert 'DIRECCION MUNICIPAL' in answer('¿Cuál es su empleo?').text
    assert 'POLICIA PREVENTIVO' in answer('¿Cuál es su cargo?').text
    assert 'DIRECCION MUNICIPAL' in answer('¿En qué corporación trabaja?').text
    assert 'OPERATIVA' in answer('¿En qué área trabaja?').text
    assert 'HERMOSILLO' in answer('¿En qué municipio trabaja?').text


def test_estado_registrado_no_es_vigencia_actual():
    result=answer('¿Sigue trabajando actualmente, está activo?')
    assert 'ACTIVO' in result.text
    assert 'no confirma' in result.text.lower()
    assert any(e['sourceCodes']==['RNPSP'] for e in result.evidence)


def test_preguntas_combinadas_y_resumen():
    result=answer('¿Qué edad tiene y en qué trabaja?')
    assert '42 años' in result.text and 'POLICIA PREVENTIVO' in result.text
    result=answer('Dame todos los datos del perfil')
    assert 'CUIP' in result.text and 'Nacionalidad' in result.text and 'Puesto' in result.text
    assert len(result.evidence)>=18


def test_no_confundir_preguntas_de_grafo_ni_analisis():
    assert answer('¿Qué personas aparecen vinculadas en el grafo?') is None
    assert answer('Analiza patrones de corrupción y riesgos') is None
    assert answer('¿Qué debería investigar sobre las relaciones del grafo?') is None


def test_no_inferir_genero():
    result=answer('¿Cuál es su identidad de género?')
    assert 'No se encuentra registrada' in result.text
    assert 'FEMENINO' not in result.text


def test_no_inferir_edad_al_ingreso():
    result=answer('¿Qué edad tenía al ingresar a trabajar?')
    assert 'No es posible calcular' in result.text
    sample=sample_profile()
    sample['data'].append({'code':'EMPLOYMENT_FECHA_INGRESO','value':'2018-09-14','origins':[{'sourceCode':'RNPSP'}]})
    result=answer('¿Qué edad tenía al ingresar a trabajar?', profile=sample)
    assert '34 años' in result.text


def test_sin_datos_no_fabricar_valor():
    profile={'profileId':'p','data':[]}
    result=answer('¿Cuál es su edad?',profile=profile)
    assert 'No hay una fecha de nacimiento válida' in result.text


def test_nuevos_campos_son_extensibles():
    profile=sample_profile()
    profile['data'].append({'code':'OTRO_ATRIBUTO_ESPECIAL','value':'DATO CONFIRMADO','origins':[{'sourceCode':'RNPSP'}]})
    result=answer('¿Cuál es su otro atributo especial?',profile=profile)
    assert result and 'DATO CONFIRMADO' in result.text


def test_instalador_preserva_flujo_existente():
    from APLICAR_PERFIL_LINCE import new_analyzer
    source = '''from .fast_router import FastQueryRouter

def stream_inline(self, request):
    profile = request.profile.model_dump(mode="json")
    if True:
        # El Fast Router habitual devuelve el primer valor disponible.
        return profile

def stream_case(self, case_id, request):
    pass
'''
    # Aquí se comprueba compatibilidad de anclajes; la prueba Angular/API real
    # corresponde al entorno de ejecución con el repositorio completo.
    updated = new_analyzer(source)
    assert updated.count('answer_profile_question(profile, request.question)') == 1
    assert updated.index('factual = answer_profile_question') < updated.index('        # El Fast Router habitual')
    assert 'def stream_case(' in updated
    from pytest import raises
    with raises(ValueError):
        new_analyzer(updated)


def test_cotejo_curp_identifica_incompatibilidad_sin_inferir_culpa():
    profile=sample_profile()
    for item in profile['data']:
        if item['code']=='CURP':
            item['value']='PEGJ961202HDFZZZ00'
    result=answer('¿La CURP coincide con la fecha y sexo registrados?',profile=profile)
    assert result is not None
    assert 'Diferencias documentales' in result.text
    assert '961202' in result.text
    assert 'FEMENINO' in result.text
    assert 'documentos originales' in result.text
    assert len(result.evidence)==3
