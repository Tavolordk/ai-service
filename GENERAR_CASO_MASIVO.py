#!/usr/bin/env python3
"""Datos 100% ficticios. Genera JSON con N vínculos SIN mantenerlos en RAM.

Uso: python GENERAR_CASO_MASIVO.py --links 1000000 --output caso_1m.json
Requisitos de disco: espacio para JSON, índice SQLite y carpeta temporal.
"""
import argparse
import json
from pathlib import Path


def generate(path: Path, links: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as handle:
        handle.write('{"profile":{"profileId":"PERFIL-SIMULADO","data":[')
        handle.write(json.dumps({'dataType':'NOMBRE','value':'PERSONA FICTICIA'},ensure_ascii=False))
        handle.write(']},"graph":{"nodes":[')
        handle.write(json.dumps({'id':'CENTRO','title':'PERSONA FICTICIA','type':'Person'},ensure_ascii=False))
        handle.write('],"links":[')
        for index in range(links):
            if index:
                handle.write(',')
            # Usar vecinos recurrentes para probar nodo con muchísimos vínculos.
            target=f'VEH-{index % 20000:05d}'
            handle.write(json.dumps({'source':'CENTRO','target':target,'type':'RELACION_SIMULADA',
                                     'referencia':f'FICT-{index:08d}'},ensure_ascii=False,separators=(',',':')))
        handle.write(']}}')


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--links',type=int,default=100000,help='1000, 100000, 1000000...')
    parser.add_argument('--output',type=Path,default=Path('caso_masivo_ficticio.json'))
    args=parser.parse_args()
    if not (1 <= args.links <= 10000000):
        parser.error('--links debe estar entre 1 y 10000000')
    generate(args.output,args.links)
    print(f'Archivo generado: {args.output} | vínculos: {args.links:,} | bytes: {args.output.stat().st_size:,}')
