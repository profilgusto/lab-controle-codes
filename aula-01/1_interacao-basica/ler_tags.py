"""Leitura das tags da planta TQ CE117 via EtherNet/IP (pycomm3).

Le de uma vez as seis tags expostas a rede (Tab. 1.3 do roteiro) e mostra cada
uma em contas e em volts, aplicando a Eq. (1) da Aula 1.

Uso (a partir da pasta aula-01/):
    python3 -W ignore 1_interacao-basica/ler_tags.py
"""

import os
import sys

from pycomm3 import LogixDriver

# `conversoes.py` fica em `comum/`, na raiz do repositorio: dois niveis acima
# da pasta deste script.
RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(RAIZ, 'comum'))

from conversoes import conta_para_volts, contas_para_altura

PLC_IP = '200.200.200.25'

TAGS = [
    'Program:MainProgram.PUMP2_DAC',
    'Program:MainProgram.VALVE_DAC',
    'Program:MainProgram.FT2_ADC',
    'Program:MainProgram.PT_ADC',
    'Program:MainProgram.TT5_ADC',
    'Program:MainProgram.LT_ADC',
]


if __name__ == '__main__':
    with LogixDriver(PLC_IP) as plc:
        print('CLP:', plc.info.get('product_name'))
        for r in plc.read(*TAGS):
            if r.error:
                print(f'  {r.tag:38s} ERRO: {r.error}')
            else:
                linha = (f'  {r.tag:38s} = {r.value:6d} contas '
                         f'= {conta_para_volts(r.value):+6.3f} V')
                if r.tag.endswith('LT_ADC'):
                    # So faz sentido depois de LT_A/LT_B calibrados na
                    # Secao 1.3.4; ate la, a reta e a nominal.
                    linha += (f' = {contas_para_altura(r.value):+7.1f} mm'
                              ' (reta de conversoes.py)')
                print(linha)
