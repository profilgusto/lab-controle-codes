"""Leitura das tags da planta TQ CE117 via EtherNet/IP (pycomm3).

Uso (a partir da raiz do projeto):
    PYTHONPATH=libs python3 -W ignore 1_interacao-basica/ler_tags.py
"""

from pycomm3 import LogixDriver

PLC_IP = '200.200.200.25'

TAGS = [
    'Program:MainProgram.PUMP2_DAC',
    'Program:MainProgram.VALVE_DAC',
    'Program:MainProgram.FT2_ADC',
    'Program:MainProgram.PT_ADC',
    'Program:MainProgram.TT5_ADC',
    'Program:MainProgram.LT_ADC',
]


def conta_para_volts(conta):
    """Cartao AD/DA: -32768..32767 contas <-> -10.5..+10.5 V."""
    return conta * 10.5 / 32768


def volts_para_conta(volts):
    return int(round(volts * 32768 / 10.5))


if __name__ == '__main__':
    with LogixDriver(PLC_IP) as plc:
        print('CLP:', plc.info.get('product_name'))
        for r in plc.read(*TAGS):
            if r.error:
                print(f'  {r.tag:38s} ERRO: {r.error}')
            else:
                print(f'  {r.tag:38s} = {r.value:6d} contas '
                      f'= {conta_para_volts(r.value):+6.3f} V')
