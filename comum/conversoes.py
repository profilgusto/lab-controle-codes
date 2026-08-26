"""Conversoes entre contas do CLP e grandezas de engenharia da planta TQ CE117.

Concentra as conversoes da Eq. (1) e da Eq. (2) da Aula 1 (cartao AD/DA) e a
curva de calibracao de LT levantada experimentalmente na Aula 1.

Modulo compartilhado por todas as aulas: mora em `comum/`, na raiz do
repositorio, e nao depende de nada fora da biblioteca padrao. Os scripts o
alcancam acrescentando `comum/` ao `sys.path` e importando pelo nome:

    import os, sys
    RAIZ = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, os.path.join(RAIZ, 'comum'))

    from conversoes import contas_para_altura

Para conferir a tabela de conversao, rode o modulo direto:

    python3 comum/conversoes.py
"""

# --- cartao analogico do CompactLogix L24EPR -------------------------------
# -32768..32767 contas <-> -10,5..+10,5 V
V_FUNDO_CARTAO = 10.5
CONTAS_FUNDO_CARTAO = 32768

# Os instrumentos trabalham de 0 a 10 V, e nao ate o fundo de escala do
# cartao: 10 V correspondem a 31207 contas, e nao a 32767. E o numero da OBS
# da Secao 1.1.3 da Aula 1 - um sensor saturado indica 31207, e 100 % de
# comando sao 31207 contas.
V_FUNDO_INSTRUMENTO = 10.0
CONTAS_FUNDO_INSTRUMENTO = 31207  # trunc(32768 * 10,0 / 10,5)
INT_MIN = 0
INT_MAX = 32767  # 2**15 - 1: escrever 32768 estoura o INT e vira -32768

# --- calibracao do transmissor de nivel LT ---------------------------------
# Reta h[mm] = LT_A * V + LT_B ajustada aos pontos da Tab. 1.5 (Aula 1).
# >>> SUBSTITUA pelos coeficientes que a SUA bancada produziu. <<<
# Os valores abaixo sao apenas o comportamento NOMINAL da Tab. 1.1 (0 V tanque
# vazio, 10 V tanque cheio), com os ~180 mm de coluna util observados no ensaio
# piloto da Aula 2. Servem so para o modo --sim.
LT_A = 18.0   # mm por volt
LT_B = 0.0    # mm

# --- transmissor de vazao FT2 ----------------------------------------------
FT2_LPM_POR_VOLT = 1.0  # 1 L/min por volt (Tab. 1.1 da Aula 1)


def conta_para_volts(conta):
    """Eq. (1): converte a conta lida pelo cartao em tensao, em volts."""
    return conta * V_FUNDO_CARTAO / CONTAS_FUNDO_CARTAO


def volts_para_conta(volts):
    """Eq. (2): converte uma tensao de comando na conta a ser escrita.

    Satura em [0, 32767] para nunca transbordar o INT do Logix.
    """
    conta = int(round(volts * CONTAS_FUNDO_CARTAO / V_FUNDO_CARTAO))
    return max(INT_MIN, min(INT_MAX, conta))


def contas_para_altura(conta):
    """Converte a leitura de LT_ADC, em contas, na altura h do nivel, em mm.

    Usa a reta de calibracao levantada na Aula 1, e nao a conversao nominal
    em porcentagem de enchimento.
    """
    return LT_A * conta_para_volts(conta) + LT_B


def contas_para_vazao(conta):
    """Converte a leitura de FT2_ADC, em contas, na vazao q_in, em L/min."""
    return FT2_LPM_POR_VOLT * conta_para_volts(conta)


def percentual_para_conta(percentual):
    """Converte um comando de 0 a 100 % na conta a escrever no DAC.

    100 % corresponde a 10 V, o fundo de escala do INSTRUMENTO, e nao aos
    10,5 V do cartao: 100 % sao 31207 contas, nao 32767. A escala usa
    CONTAS_FUNDO_INSTRUMENTO direto para que 100 % caia exatamente nas 31207
    contas do roteiro (a conversao por volts arredondaria para 31208).
    """
    percentual = max(0.0, min(100.0, float(percentual)))
    conta = int(round(percentual / 100.0 * CONTAS_FUNDO_INSTRUMENTO))
    return max(INT_MIN, min(INT_MAX, conta))


def conta_para_percentual(conta):
    """Inversa de `percentual_para_conta`: conta do DAC -> comando em %."""
    return conta / CONTAS_FUNDO_INSTRUMENTO * 100.0


if __name__ == '__main__':
    print(f'{"contas":>8} {"volts":>8} {"h [mm]":>8} {"q [L/min]":>10}')
    for conta in (0, 3121, 6241, 12483, 18724, 24966, 31207):
        print(f'{conta:8d} {conta_para_volts(conta):8.3f} '
              f'{contas_para_altura(conta):8.2f} {contas_para_vazao(conta):10.3f}')
