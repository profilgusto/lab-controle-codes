"""Escreve VALVE_DAC e PUMP2_DAC, aguarda 2 s e le todas as tags da planta.

E o codigo usado na Secao 1.3.1 (passo 3.3) e nas condicoes iniciais da
Secao 1.3.2 do roteiro, onde a tarefa e "abrir em 100 % a valvula S":

    python3 -W ignore 1_interacao-basica/atua_e_le.py --pct 100 0

Sem `--pct`, os valores sao contas do DAC, como no Cod. 1.1 do roteiro
(31207 contas = 10 V = 100 % do fundo de escala do instrumento):

    python3 -W ignore 1_interacao-basica/atua_e_le.py 31207 0
    python3 -W ignore 1_interacao-basica/atua_e_le.py          (pergunta na tela)

Intertravamento (OBS da Secao 1.2.3): o programa recusa acionar PUMP2 com a
valvula S fechada, e a escrita manda sempre S na frente da bomba.

Uso a partir da pasta aula-01/.
"""

import os
import sys
import time

from pycomm3 import LogixDriver

# `conversoes.py` fica em `comum/`, na raiz do repositorio: dois niveis acima
# da pasta deste script.
RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(RAIZ, 'comum'))

# INT_MIN/INT_MAX (0 e 32767, o maior valor que cabe num INT do Logix) vem do
# modulo compartilhado, para nao existirem duas copias do mesmo limite.
from conversoes import (INT_MIN, INT_MAX, conta_para_percentual,
                        conta_para_volts, percentual_para_conta)

PLC_IP = '200.200.200.25'
ESPERA_S = 2.0

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


def valida_pct(nome, texto):
    """Converte um comando em % na conta do DAC (100 % = 10 V = 31207)."""
    try:
        valor = float(texto)
    except ValueError:
        sys.exit(f'ERRO: {nome} = "{texto}" nao e um numero.')

    if not 0.0 <= valor <= 100.0:
        sys.exit(f'ERRO: {nome} = {valor} fora da faixa [0, 100] %.')
    return percentual_para_conta(valor)


def le_argumentos():
    argv = sys.argv[1:]
    em_pct = argv and argv[0] == '--pct'
    if em_pct:
        argv = argv[1:]

    converte = valida_pct if em_pct else valida
    unidade = '%' if em_pct else 'contas'
    faixa = '0..100' if em_pct else f'{INT_MIN}..{INT_MAX}'

    if len(argv) == 2:
        return converte('VALVE', argv[0]), converte('PUMP2', argv[1])
    if not argv:
        return (converte('VALVE', input(f'VALVE [{faixa}] {unidade}: ')),
                converte('PUMP2', input(f'PUMP2 [{faixa}] {unidade}: ')))
    sys.exit(f'Uso: {sys.argv[0]} [--pct] <VALVE> <PUMP2>')


def main():
    valve, pump2 = le_argumentos()

    # Intertravamento da OBS da Secao 1.2.3: acionar PUMP2 contra a valvula S
    # fechada pressuriza a linha e pode danificar a planta. O CLP nao impede,
    # entao a trava mora aqui.
    if pump2 > 0 and valve == 0:
        sys.exit('ERRO: PUMP2 acionado com a valvula S fechada. Abra S antes '
                 '(por exemplo: --pct 100 0) e so entao acione a bomba.')

    with LogixDriver(PLC_IP) as plc:
        print(f'Conectado em {PLC_IP} ({plc.info.get("product_name")})\n')

        # --- escrita ---------------------------------------------------
        print(f'Escrevendo  VALVE_DAC = {valve:6d} '
              f'({conta_para_percentual(valve):5.1f} %)  |  '
              f'PUMP2_DAC = {pump2:6d} ({conta_para_percentual(pump2):5.1f} %)')
        # A valvula vai sempre na frente da bomba, nesta ordem.
        resultados = plc.write((TAG_VALVE, valve), (TAG_PUMP2, pump2))
        for r in resultados:
            if r.error:
                sys.exit(f'ERRO ao escrever em {r.tag}: {r.error}')
        print('Escrita confirmada pelo CLP.')

        # --- espera ----------------------------------------------------
        print(f'Aguardando {ESPERA_S:.0f} s...\n')
        time.sleep(ESPERA_S)

        # --- leitura ---------------------------------------------------
        print(f'{"TAG":<38} {"VALOR (INT)":>12} {"VOLTS":>8}')
        print('-' * 60)
        for r in plc.read(*TAGS_LEITURA):
            if r.error:
                print(f'{r.tag:<38} {"ERRO: " + r.error:>12}')
            else:
                print(f'{r.tag:<38} {r.value:>12d} '
                      f'{conta_para_volts(r.value):>+8.3f}')


if __name__ == '__main__':
    main()
