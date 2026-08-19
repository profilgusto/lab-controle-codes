"""Escreve VALVE_DAC e PUMP2_DAC, aguarda 2 s e le todas as tags da planta.

Uso (a partir da raiz do projeto):
    PYTHONPATH=libs python3 -W ignore 1_interacao-basica/atua_e_le.py <VALVE_DAC> <PUMP2_DAC>
    PYTHONPATH=libs python3 -W ignore 1_interacao-basica/atua_e_le.py   (pergunta na tela)
"""

import sys
import time

from pycomm3 import LogixDriver

PLC_IP = '200.200.200.25'
ESPERA_S = 2.0

INT_MIN = 0
INT_MAX = 32767  # 2**15 - 1: o maior valor que cabe num INT do Logix

TAG_VALVE = 'Program:MainProgram.VALVE_DAC'
TAG_PUMP2 = 'Program:MainProgram.PUMP2_DAC'

TAGS_LEITURA = [
    TAG_PUMP2,
    TAG_VALVE,
    'Program:MainProgram.FT2_ADC',
    'Program:MainProgram.PT_ADC',
    'Program:MainProgram.TT5_ADC',
    'Program:MainProgram.LT_ADC',
]


def valida(nome, texto):
    """Converte para int e garante que cabe na faixa de um INT positivo."""
    try:
        valor = int(texto)
    except ValueError:
        sys.exit(f'ERRO: {nome} = "{texto}" nao e um numero inteiro.')

    if not INT_MIN <= valor <= INT_MAX:
        sys.exit(f'ERRO: {nome} = {valor} fora da faixa '
                 f'[{INT_MIN}, {INT_MAX}]. Lembre que 2**15 = 32768 '
                 f'estoura o INT e viraria -32768 no CLP.')
    return valor


def le_argumentos():
    if len(sys.argv) == 3:
        return valida('VALVE_DAC', sys.argv[1]), valida('PUMP2_DAC', sys.argv[2])
    if len(sys.argv) == 1:
        return (valida('VALVE_DAC', input(f'VALVE_DAC [{INT_MIN}..{INT_MAX}]: ')),
                valida('PUMP2_DAC', input(f'PUMP2_DAC [{INT_MIN}..{INT_MAX}]: ')))
    sys.exit(f'Uso: {sys.argv[0]} <VALVE_DAC> <PUMP2_DAC>')


def main():
    valve, pump2 = le_argumentos()

    with LogixDriver(PLC_IP) as plc:
        print(f'Conectado em {PLC_IP} ({plc.info.get("product_name")})\n')

        # --- escrita ---------------------------------------------------
        print(f'Escrevendo  VALVE_DAC = {valve}  |  PUMP2_DAC = {pump2}')
        resultados = plc.write((TAG_VALVE, valve), (TAG_PUMP2, pump2))
        for r in resultados:
            if r.error:
                sys.exit(f'ERRO ao escrever em {r.tag}: {r.error}')
        print('Escrita confirmada pelo CLP.')

        # --- espera ----------------------------------------------------
        print(f'Aguardando {ESPERA_S:.0f} s...\n')
        time.sleep(ESPERA_S)

        # --- leitura ---------------------------------------------------
        print(f'{"TAG":<38} {"VALOR (INT)":>12}')
        print('-' * 51)
        for r in plc.read(*TAGS_LEITURA):
            if r.error:
                print(f'{r.tag:<38} {"ERRO: " + r.error:>12}')
            else:
                print(f'{r.tag:<38} {r.value:>12d}')


if __name__ == '__main__':
    main()
