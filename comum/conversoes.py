"""Conversoes entre contas do CLP e grandezas de engenharia da planta TQ CE117.

Concentra as conversoes da Eq. (1) e da Eq. (2) da Aula 1 (cartao AD/DA) e a
curva de calibracao de LT levantada experimentalmente na Aula 1.

O transmissor de nivel LT e classificado como NAO LINEAR pelo fabricante
(Tab. 1, p. 16 de tecquipment2008), ao contrario de TT, FT e PT. A conversao
`contas_para_altura` usa por isso um polinomio (nao uma reta) ajustado aos
pontos da Tab. 1.6 (calibracao de LT, Aula 1); ver `ferramentas/calibracao-lt/
calibra_lt.py` no repositorio de codigos para reajustar esses coeficientes com
os dados da sua propria bancada.

Modulo compartilhado por todas as aulas: mora em `comum/`, na raiz do
repositorio, e nao depende de nada fora da biblioteca padrao (os scripts de
calibracao usam numpy, mas so para *gerar* os coeficientes abaixo; em tempo de
execucao este modulo so faz avaliacao de polinomio, sem numpy). Os scripts o
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
# LT e NAO LINEAR (Tab. 1 do manual, tecquipment2008): uma reta erra ate ~26 mm
# (~13 % do fundo de 200 mm) nos dados piloto, contra ~2 a 3 mm de um cubico.
# `contas_para_altura` por isso avalia um polinomio de 3o grau em h(contas),
# nao uma reta; `altura_para_contas` e o polinomio inverso h(contas) -> contas,
# usado so pelo simulador `--sim`, que precisa gerar leituras a partir de um h
# conhecido.
#
# Os coeficientes abaixo (mm por conta^3..0, e conta por mm^3..0) vem do
# ajuste cubico aos 20 pontos piloto de esvaziamento da Tab. 1.6 da Aula 1
# (h de 10 a 200 mm em passos de 10 mm, vazao nula em cada ponto).
# >>> SUBSTITUA pelos coeficientes que a SUA bancada produziu, com
# `ferramentas/calibracao-lt/calibra_lt.py`. <<<
LT_COEFS_H_DE_CONTAS = (6.202235476946863e-12, -1.726047648882776e-07,
                        0.005574400874448534, -4.043696100608721)
LT_COEFS_CONTAS_DE_H = (0.0006561251225405194, -0.8058853830693901,
                        295.18035453277054, -429.73787409700026)

# Ajuste valido so dentro da faixa calibrada (h de 10 a 200 mm nos dados
# piloto); fora dela e extrapolacao do polinomio, sem garantia fisica.


def _horner(coefs, x):
    """Avalia um polinomio em `x` a partir dos coeficientes, do maior grau ao menor."""
    resultado = 0.0
    for c in coefs:
        resultado = resultado * x + c
    return resultado

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

    Usa o polinomio cubico de calibracao levantado na Aula 1 (LT e um sensor
    NAO LINEAR, ver comentario acima), e nao a conversao nominal em
    porcentagem de enchimento nem uma reta.
    """
    return _horner(LT_COEFS_H_DE_CONTAS, conta)


def altura_para_contas(h_mm):
    """Inversa aproximada de `contas_para_altura`: h [mm] -> contas de LT_ADC.

    Usada apenas pelo simulador (`--sim`), que precisa gerar uma leitura de
    LT a partir do h fisico simulado. Nao e a inversa algebrica exata do
    polinomio de `contas_para_altura`, mas um segundo ajuste cubico
    independente na direcao h -> contas, sobre os mesmos pontos piloto.
    """
    return _horner(LT_COEFS_CONTAS_DE_H, h_mm)


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
