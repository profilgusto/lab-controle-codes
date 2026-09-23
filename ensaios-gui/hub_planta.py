"""Hub de ensaios da planta TQ CE117: uma unica GUI para todas as aulas.

Problema que este programa resolve: cada aula tinha seus proprios scripts de
linha de comando, cada um abrindo (e fechando) sua propria conexao com o CLP.
Na pratica, os alunos ficavam trocando de terminal e de script o tempo
inteiro - ler tags aqui, atuar acola, gravar um ensaio noutro. O hub junta
tudo isso numa unica janela:

  - uma UNICA conexao com o CLP, mantida por uma thread de aquisicao que le
    todas as tags em ciclo e escreve os comandos pendentes;
  - um grafico ao vivo com LT, FT2, PUMP2 e VALVE (as quatro em % do fundo de
    escala do instrumento, a mesma unidade de `conta_para_percentual` em
    `comum/conversoes.py` - por isso cabem no mesmo eixo);
  - dois sliders (e um botao ON/OFF ao lado de cada um, em harmonia com o
    slider) para atuar em VALVE e PUMP2, com o mesmo intertravamento de
    seguranca: PUMP2 so fica ligada com VALVE em pelo menos
    `VALVE_MIN_COM_BOMBA` (30 %);
  - um `ttk.Notebook` com uma aba por aula; cada aba tem as ferramentas de
    ensaio daquela aula (por enquanto, Aulas 1 a 3 - as demais aparecem como
    "em desenvolvimento", no mesmo estado do roteiro).

Toda aba que precisa gravar um CSV (calibracao de LT, degrau) o faz a partir
do MESMO fluxo de amostras da thread de aquisicao - nao abre uma
segunda conexao com o CLP.

Uso (a partir da pasta ensaios-gui/):
    python3 -W ignore hub_planta.py
    python3 -W ignore hub_planta.py --sim   (sem CLP, tanque simulado)
    python3 -W ignore hub_planta.py --ip 200.200.200.25

Requisito: tkinter (sistema, nao vem do pip) e numpy (para a calibracao de
LT, na Aba 1, e para o ajuste de Torricelli, na Aba 2). matplotlib e scipy
sao opcionais, importados sob demanda pelos botoes de exportar grafico
(Aba 1), "exportar dados" (PDF) e "Exportar Graficos do Ensaio de
Esvaziamento" (Aba 2, este ultimo tambem exige scipy para o ajuste dos
modelos de Torricelli) - sem eles instalados, o resto do hub funciona
normalmente. Ver `requirements.txt`, em `lab-controle-codes/`.
"""

import argparse
import csv
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from collections import deque
from tkinter import filedialog, messagebox, ttk

# `conversoes.py` fica em `comum/`, na raiz do repositorio: um nivel acima
# da pasta deste script.
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RAIZ, 'comum'))

from conversoes import (INT_MAX, INT_MIN, LT_COEFS_H_DE_CONTAS,
                        conta_para_percentual, conta_para_volts,
                        contas_para_altura, contas_para_vazao,
                        percentual_para_conta)

PLC_IP = '200.200.200.25'

PERIODO_S = 0.5       # intervalo entre leituras da thread de aquisicao
JANELA_S = 60.0        # largura inicial da janela de tempo do grafico
JANELAS = (('30 s', 30.0), ('1 min', 60.0), ('2 min', 120.0), ('5 min', 300.0),
           ('10 min', 600.0), ('30 min', 1800.0), ('60 min', 3600.0),
           ('90 min', 5400.0))
JANELA_MAX_S = max(s for _r, s in JANELAS)  # tambem o teto do historico em
                                             # memoria (Aquisicao/Janela._historico
                                             # abaixo) - 90 min, para cobrir a
                                             # escada de degraus da Aula 3 (~50 min)

TAG_PUMP2 = 'Program:MainProgram.PUMP2_DAC'
TAG_VALVE = 'Program:MainProgram.VALVE_DAC'
TAG_FT2 = 'Program:MainProgram.FT2_ADC'
TAG_PT = 'Program:MainProgram.PT_ADC'
TAG_TT5 = 'Program:MainProgram.TT5_ADC'
TAG_LT = 'Program:MainProgram.LT_ADC'

TAGS_LEITURA = (TAG_LT, TAG_FT2, TAG_PT, TAG_TT5, TAG_PUMP2, TAG_VALVE)

COLUNAS_ENSAIO = ['t_s', 'lt_contas', 'h_mm', 'ft2_contas', 'qin_lpm', 'pump2_pct']

# Colunas do CSV exportado a partir de uma selecao no grafico ao vivo (botao
# "exportar dados"): todas as tags lidas na janela selecionada, contas +
# volts + grandeza fisica quando ha conversao definida.
COLUNAS_EXPORTACAO = [
    't_s',
    'lt_contas', 'lt_volts', 'h_mm',
    'ft2_contas', 'ft2_volts', 'qin_lpm',
    'pt_contas', 'pt_volts',
    'tt5_contas', 'tt5_volts',
    'pump2_contas', 'pump2_volts', 'pump2_pct',
    'valve_contas', 'valve_volts', 'valve_pct',
]

# Series do grafico: chave em `valores`, rotulo, cor. Todas convertidas para
# % do fundo de escala do instrumento antes de desenhar (mesma unidade de
# `conta_para_percentual`), o que permite compartilhar um unico eixo.
# Passo dos sliders/botoes de comando manual (VALVE, PUMP2), em % do fundo
# de escala do instrumento.
PASSO_SLIDER = 0.5


def _arredonda_passo(percentual, passo=PASSO_SLIDER):
    """Arredonda um percentual (0-100) para o multiplo de `passo` mais proximo."""
    return round(round(percentual / passo) * passo, 10)


# Intertravamento VALVE x PUMP2: com PUMP2 ligada (qualquer comando > 0), VALVE
# tem de estar em pelo menos este valor, em % de abertura.
VALVE_MIN_COM_BOMBA = 30.0

SERIES_GRAFICO = (
    ('LT', 'LT (nivel)', '#0a6ebd'),
    ('FT2', 'FT2 (vazao)', '#d98c00'),
    ('PUMP2', 'PUMP2 (bomba)', '#1e9e4a'),
    ('VALVE', 'VALVE (valvula S)', '#a11'),
)

# Curvas do controlador da Aula 4, em % (mesmo eixo esquerdo das series acima):
# u (comando pedido pelo controlador, antes da saturacao), u_c (comando aplicado,
# ja saturado), u_K (termo proporcional Kc*e), u_Ti (termo
# integral que entrou no calculo de u[k]) e u_D (termo derivativo, so existe no
# PID livre do topo da janela - o checkbox some na malha da Aula 4). So sao
# desenhadas, e so ganham checkbox, enquanto uma malha (Aula 4 ou PID livre)
# esta em curso (`Grafico.ctrl_ativo`).
# u_K pode ser negativo, entao o eixo esquerdo se estende alem de -5..105 %
# quando elas estao em tela (ver `Grafico.redesenha`).
SERIES_CTRL = (
    ('u', 'u (sem saturacao)', '#4dd0e1'),
    ('uc', 'u_c (saturado)', '#00565f'),
    ('uK', 'u_K (proporcional)', '#c2185b'),
    ('uTi', 'u_Ti (integral)', '#5d4037'),
    ('uD', 'u_D (derivativo)', '#ef6c00'),
)

# Erro e = r - h, em mm: mesma natureza da curva de altura, entao vai no eixo
# secundario (mm) e nao no de %. Tambem so existe com a malha da Aula 4 em
# curso, e pode ser negativo (o eixo se estende para baixo, ver `redesenha`).
SERIE_ERRO = ('e', 'e (erro, mm)', '#3949ab')

# A curva de altura do nivel (h, em mm) nao entra em SERIES_GRAFICO: ao
# contrario das demais, ela nao e uma fracao do fundo de escala do
# instrumento (0-100 %), e sim uma grandeza fisica em mm, com faixa propria.
# Por isso o `Grafico` a desenha num eixo vertical secundario, a direita.
# Ela esta sempre disponivel, porque sempre ha uma calibracao de LT em vigor:
# a da biblioteca (`LT_COEFS_H_DE_CONTAS`, em `comum/conversoes.py`) ate que o
# botao "Ajustar calibracao de LT" do `PainelLeituras` receba os coeficientes
# levantados na propria bancada (Aula 1).
ROTULO_ALTURA = 'h (nivel, mm)'
COR_ALTURA = '#7b2d8e'

# Curva de referencia (r, mm) da malha fechada da Aula 4: compartilha o
# mesmo eixo secundario (mm) da curva de altura acima, e so aparece enquanto
# ela tambem aparecer - por isso nao tem flag propria de "disponivel", so a
# deque `Grafico.serie_referencia`, vazia ate a Aula 4 gravar o primeiro
# ponto.
ROTULO_REFERENCIA = 'r (referencia, mm)'
COR_REFERENCIA = '#666666'

# Topo FIXO do eixo de h, em mm. A escala nao acompanha mais os pontos
# visiveis: com escala movel, a mesma curva mudava de inclinacao conforme a
# janela de tempo escolhida ou o trecho em tela, e duas telas (ou dois PDF
# exportados) do mesmo ensaio nao eram comparaveis a olho - um degrau de
# 3 mm num patamar acomodado parecia tao dramatico quanto a subida inteira do
# tanque. 250 mm cobre o tanque do CE117 com folga sobre a faixa util de LT
# (~40 a 180 mm nos ensaios do curso).
ALTURA_MAX_MM = 250.0
ALTURA_PASSO_MM = 50.0   # espacamento dos rotulos do eixo de h

# Nome sugerido para o CSV consolidado da varredura estatica (Aula 3): uma
# linha por patamar, que e o arquivo de onde sai toda a analise da curva do
# atuador. O aluno escolhe pasta e nome num dialogo antes de o ensaio comecar.
ARQUIVO_VARREDURA = 'curva-atuador_dados-consolidados.csv'

# Nome sugerido para o CSV continuo da escada de degraus (Aula 3). Como na
# varredura, pasta e nome saem de um dialogo aberto no "iniciar escada"; o
# arquivo de equilibrios (Tab. 3.2) e derivado deste, com o sufixo
# `_equilibrios`.
ARQUIVO_ESCADA = 'escada_degraus.csv'

# Nome sugerido para o CSV continuo da malha fechada (Aula 4). O arquivo de
# patamares (Tab. da aba, colunas de h_ext/h_final/t_sat) e derivado deste,
# com o sufixo `_patamares`, como o `_equilibrios` da Aula 3.
ARQUIVO_MALHA = 'malha_pi.csv'

# Faixa de referencia aceita pela aba da Aula 4 (Secao pi-hub) e o limite de
# seguranca que abre a malha e desliga PUMP2 - mesmos valores do roteiro.
MALHA_REF_MIN_MM = 40.0
MALHA_REF_MAX_MM = 150.0
MALHA_H_DESARME_MM = 160.0

# PID livre (painel "Comando com Controlador PID", no topo da janela): valores
# iniciais dos campos. Kp e Ki sao os do exemplo da Aula 4 (Kc = 1,85 %/mm,
# Ti = 41 s -> Ki = Kc/Ti); Kd = 0 deixa o controlador como o PI da Aula 4.
# Colunas extras do CSV exportado do grafico, acrescentadas so quando o recorte
# tem PID livre ativo em alguma amostra (ver `PainelPID.registro`).
COLUNAS_PID_EXPORTACAO = ['pid_ativo', 'pid_Kp_pct_mm', 'pid_Ki_pct_mm_s', 'pid_Kd_pct_s_mm',
                          'pid_setpoint_mm', 'pid_T_s', 'pid_Tf_s', 'pid_umin_pct',
                          'pid_umax_pct', 'pid_anti_windup']
PID_PADRAO = {'Kp': '1.85', 'Ki': '0.045', 'Kd': '0', 'sp': '70', 'T': '2',
              'Tf': '6', 'umin': '45', 'umax': '100'}


def faixa_ajustada(valores, span_minimo, reserva):
    """(lo, hi) que enquadra `valores` com folga de 5 % de cada lado.

    `span_minimo` evita o caso degenerado de uma serie parada (todos os
    valores iguais), que daria lo == hi e divisao por zero na conversao para
    pixels; `reserva` e o que devolver quando nao ha ponto nenhum em tela.
    """
    if not valores:
        return reserva
    lo, hi = min(valores), max(valores)
    margem = max((hi - lo) * 0.05, span_minimo / 2.0)
    return lo - margem, hi + margem


def faixa_altura(v_lo, v_hi):
    """(lo, hi) do eixo de h [mm] para um eixo esquerdo que vai de `v_lo` a
    `v_hi` (em % do fundo de escala).

    O topo e sempre `ALTURA_MAX_MM`. O fundo nao e livre: o "0" de h tem de
    cair na MESMA linha horizontal que o "0 %" do eixo da esquerda, senao as
    duas escalas deixam de comparar a partir da mesma base. Como o eixo
    esquerdo reserva a faixa `v_lo`..0 abaixo do seu zero, o de h reserva a
    mesma fracao da altura do grafico abaixo do dele - dai o fundo sair
    ligeiramente negativo, e nao em 0.
    """
    frac0 = -v_lo / (v_hi - v_lo)
    return -ALTURA_MAX_MM * frac0 / (1.0 - frac0), ALTURA_MAX_MM


def _avalia_polinomio(coefs, x):
    """Avalia um polinomio em `x`, com `coefs` do maior grau ao menor
    (convencao do `numpy.polyfit`/`numpy.polyval`, a mesma usada pelo
    assistente de calibracao de LT e por `LT_COEFS_H_DE_CONTAS`)."""
    resultado = 0.0
    for c in coefs:
        resultado = resultado * x + c
    return resultado


def _formata_equacao_polinomio(coefs):
    """Monta 'h(N) = a3*N^3 + a2*N^2 + ...' a partir dos coeficientes (do
    maior grau ao menor, mesma convencao de `_avalia_polinomio`)."""
    grau = len(coefs) - 1
    termos = []
    for i, c in enumerate(coefs):
        expoente = grau - i
        if expoente == 0:
            termos.append(f'{c!r}')
        elif expoente == 1:
            termos.append(f'{c!r}*N')
        else:
            termos.append(f'{c!r}*N^{expoente}')
    return 'h(N) = ' + ' + '.join(termos).replace('+ -', '- ')


def rotulo_janela(segundos):
    for rotulo, valor in JANELAS:
        if abs(valor - segundos) < 1e-6:
            return rotulo
    return f'{segundos:.0f} s'


def rotulo_tempo(t):
    if t < 120:
        return f'{t:.0f}s'
    return f'{int(t) // 60:d}:{int(t) % 60:02d}'


def _formata_polinomio(coefs, variavel='N'):
    """Formata coefs (grau mais alto primeiro, convencao numpy) como
    'h(N) = a_n N^n + ... + a_1 N + a_0', com sinais e notacao cientifica."""
    grau = len(coefs) - 1
    termos = []
    for i, a in enumerate(coefs):
        expoente = grau - i
        sinal = '-' if a < 0 else '+'
        if expoente == 0:
            corpo = f'{abs(a):.4e}'
        elif expoente == 1:
            corpo = f'{abs(a):.4e}·{variavel}'
        else:
            corpo = f'{abs(a):.4e}·{variavel}^{expoente}'
        termos.append((sinal, corpo))

    primeiro_sinal, primeiro_corpo = termos[0]
    expressao = ('-' if primeiro_sinal == '-' else '') + primeiro_corpo
    for sinal, corpo in termos[1:]:
        expressao += f' {sinal} {corpo}'
    return f'h({variavel}) = {expressao}  [mm]'


# ---------------------------------------------------------------------------
# Acesso a planta
# ---------------------------------------------------------------------------

class PlantaCLP:
    """Le e escreve as tags do CLP real. Uma unica instancia, uma unica conexao."""

    def __init__(self, ip):
        self.ip = ip
        self.plc = None

    def conecta(self):
        from pycomm3 import LogixDriver

        self.plc = LogixDriver(self.ip)
        self.plc.open()
        return self.plc.info.get('product_name') or self.ip

    def fecha(self):
        if self.plc is not None:
            try:
                self.plc.close()
            finally:
                self.plc = None

    def escreve(self, valve, pump2):
        # A valvula vai sempre na frente da bomba (intertravamento: VALVE >= 30 % com PUMP2 ligada).
        for r in self.plc.write((TAG_VALVE, valve), (TAG_PUMP2, pump2)):
            if r.error:
                raise RuntimeError(f'escrita em {r.tag}: {r.error}')

    def le(self):
        """Devolve um dict {LT, FT2, PT, TT5, PUMP2, VALVE} em contas."""
        leituras = {}
        for r in self.plc.read(*TAGS_LEITURA):
            if r.error:
                raise RuntimeError(f'leitura de {r.tag}: {r.error}')
            leituras[r.tag] = r.value
        return {
            'LT': leituras[TAG_LT], 'FT2': leituras[TAG_FT2],
            'PT': leituras[TAG_PT], 'TT5': leituras[TAG_TT5],
            'PUMP2': leituras[TAG_PUMP2], 'VALVE': leituras[TAG_VALVE],
        }


class PlantaSimulada:
    """Tanque de Torricelli de brincadeira, para testar a GUI sem a planta.

    Parametros calibrados para reproduzir os numeros REAIS medidos na bancada
    com o dreno totalmente aberto (ver `_refs/briefing-ensaios-aula2.md` e
    `_refs/briefing-dreno-restrito-2026-09-09.md` no repositorio do roteiro):
    equilibrio ~180 mm com PUMP2/VALVE a 100 %, e uma lei de Torricelli
    GENERALIZADA com offset de carga, q_out = K * sqrt(h + C_MM), pois a
    planta real mostrou uma nao linearidade bem mais fraca que a Torricelli
    idealizada (expoente medido ~0,08, nao 0,5). Isso da tau ~ 110-140 s na
    faixa de operacao usual (h0 entre 30 e 180 mm), NAO os ~20 s de uma
    estimativa antiga baseada em Torricelli pura - contava, a versao anterior
    deste simulador, deixava o "--sim" varias vezes mais rapido que a planta
    real, o que ensina o timing errado do ensaio de degrau.
    """

    AREA_MM2 = 1.64e4       # ~164 cm^2, ajuste conjunto aos ensaios reais
    K = 3.83e3               # mm^2.5/s, em q_out = K sqrt(h + C_MM)
    C_MM = 140.0              # offset de carga do dreno, em mm
    VAZAO_MAX_MM3_S = 6.86e4  # PUMP2 e VALVE a 100 %, calibrado p/ h_eq ~180 mm

    def __init__(self, ip=None):
        self.h = 0.0
        self.pump2_pct = 0.0
        self.valve_pct = 0.0
        self.t_ultimo = time.monotonic()

    def conecta(self):
        return 'SIMULADOR (sem CLP)'

    def fecha(self):
        pass

    def escreve(self, valve, pump2):
        self.valve_pct = conta_para_percentual(valve)
        self.pump2_pct = conta_para_percentual(pump2)

    def le(self):
        from conversoes import altura_para_contas, volts_para_conta, FT2_LPM_POR_VOLT

        agora = time.monotonic()
        dt, self.t_ultimo = agora - self.t_ultimo, agora
        dt = max(0.0, min(dt, 1.0))

        q_in = (self.pump2_pct / 100.0) * (self.valve_pct / 100.0) * self.VAZAO_MAX_MM3_S
        q_out = self.K * math.sqrt(max(0.0, self.h + self.C_MM))
        self.h = max(0.0, self.h + dt * (q_in - q_out) / self.AREA_MM2)

        lt = int(round(altura_para_contas(self.h)))
        q_lpm = q_in * 60.0 / 1.0e6
        ft2 = volts_para_conta(q_lpm / FT2_LPM_POR_VOLT)
        return {
            'LT': max(INT_MIN, min(INT_MAX, lt)), 'FT2': ft2,
            'PT': int(0.3 * CONTAS_100PCT_SIM), 'TT5': int(0.45 * CONTAS_100PCT_SIM),
            'PUMP2': percentual_para_conta(self.pump2_pct),
            'VALVE': percentual_para_conta(self.valve_pct),
        }


CONTAS_100PCT_SIM = 31207


class Aquisicao(threading.Thread):
    """Thread unica dona da conexao com o CLP.

    Le todas as tags em ciclo e escreve o ultimo par (VALVE, PUMP2) pedido
    por `setpoint()`. Toda aba do hub que precisa atuar na planta - seja o
    slider manual, seja um ensaio automatico (ex.: degrau) - passa pelo
    mesmo `setpoint()`; nao ha conexoes concorrentes.
    """

    def __init__(self, planta, fila):
        super().__init__(daemon=True)
        self.planta = planta
        self.fila = fila
        self.parar = threading.Event()
        self.zerar_ao_sair = True

        self._lock = threading.Lock()
        self._alvo = (0, 0)          # (valve, pump2) em contas
        self._pendente = True

    def setpoint(self, valve, pump2):
        with self._lock:
            self._alvo = (valve, pump2)
            self._pendente = True

    def _consome_alvo(self):
        with self._lock:
            if not self._pendente:
                return None
            self._pendente = False
            return self._alvo

    def _aviso(self, texto, ok=False):
        self.fila.put(('status', texto, ok))

    def run(self):
        t0 = time.monotonic()
        conectado = False
        try:
            while not self.parar.is_set():
                try:
                    if not conectado:
                        self._aviso('conectando...')
                        nome = self.planta.conecta()
                        conectado = True
                        self._aviso(f'conectado: {nome}', ok=True)
                        with self._lock:
                            self._pendente = True

                    alvo = self._consome_alvo()
                    if alvo is not None:
                        self.planta.escreve(*alvo)

                    valores = self.planta.le()
                    self.fila.put(('amostra', time.monotonic() - t0, valores))

                except Exception as erro:                # noqa: BLE001
                    conectado = False
                    self.planta.fecha()
                    self._aviso(f'falha: {erro}')
                    self.parar.wait(2.0)
                    continue

                self.parar.wait(PERIODO_S)
        finally:
            if conectado and self.zerar_ao_sair:
                try:
                    self.planta.escreve(0, 0)
                except Exception:                        # noqa: BLE001
                    pass
            self.planta.fecha()


# ---------------------------------------------------------------------------
# Grafico: LT, FT2, PUMP2 e VALVE no mesmo eixo (% do fundo de escala)
# ---------------------------------------------------------------------------

class Grafico(tk.Canvas):

    MARGEM = (46, 30, 46, 30)   # esquerda, topo, direita, base
    # a margem direita so e usada de verdade quando a curva de altura (eixo
    # secundario, em mm) esta disponivel; sobra em branco caso contrario.

    def __init__(self, master, janela_s=JANELA_S, **kw):
        super().__init__(master, background='white', highlightthickness=1,
                         highlightbackground='#b0b0b0', **kw)
        self.series = {chave: deque() for chave, _r, _c in SERIES_GRAFICO + SERIES_CTRL + (SERIE_ERRO,)}
        self.visiveis = {chave: True for chave, _r, _c in SERIES_GRAFICO + SERIES_CTRL + (SERIE_ERRO,)}
        # Curvas de SERIES_CTRL: so entram no desenho com a malha da Aula 4 em
        # curso; fora disso ficam em memoria, mas escondidas.
        self.ctrl_ativo = False
        self.janela_s = float(janela_s)
        self._fonte_legenda = tkfont.Font(font=('TkDefaultFont', 8))

        # Curva de altura (h, mm), com eixo proprio - ver comentario de
        # ROTULO_ALTURA/COR_ALTURA. Ja nasce disponivel: ha sempre uma
        # calibracao de LT em vigor (a da biblioteca, em `conversoes.py`, ate
        # que o aluno cole a da propria bancada), e e ela que a tabela de
        # leituras ja usa para mostrar LT em mm. Esconder a curva ate uma
        # calibracao ser colada deixaria o grafico dizendo menos do que a
        # tabela ao lado dele.
        self.serie_altura = deque()
        self.altura_disponivel = True
        self.altura_visivel = True

        # Curva de referencia (r, mm) da malha fechada da Aula 4 - ver
        # comentario de ROTULO_REFERENCIA/COR_REFERENCIA. Ao contrario da
        # curva de altura, comeca vazia e so ganha pontos enquanto a aba
        # Aula 4 tem uma malha em curso; fica visivel depois de terminado o
        # ensaio, como as demais curvas, ate o grafico ser limpo ou uma nova
        # malha comecar (`limpa_referencia`, chamado por `AbaAula4`).
        self.serie_referencia = deque()

        # Modo de visualizacao: 'linha' interpola os pontos amostrados com
        # uma reta cheia (comportamento historico); 'dispersao' marca cada
        # leitura com um ponto grande e visivel, ligado por uma linha fina e
        # semi-transparente em "segurador de ordem zero" (o valor e mantido
        # constante ate a proxima amostra, com subida abrupta no instante da
        # amostra - reflete o que o CLP realmente entrega, sem sugerir uma
        # interpolacao linear entre leituras que nao existe).
        self.modo = 'dispersao'

        # Modo de escala dos dois eixos verticais. False (padrao) = faixas
        # FIXAS, -5 a 105 % a esquerda e 0 a ALTURA_MAX_MM a direita: a mesma
        # curva tem sempre a mesma inclinacao, e duas telas do mesmo ensaio
        # sao comparaveis a olho. True = cada eixo se ajusta ao minimo e ao
        # maximo do que esta EM TELA (a janela de tempo corrente, e so as
        # series marcadas em "mostrar"), para enxergar detalhe dentro de um
        # patamar acomodado. No modo adaptativo os dois zeros deixam de estar
        # alinhados - ver `faixa_altura`.
        self.escala_adaptativa = False

        # Deslocamento horizontal ("scroll") dentro do historico em memoria.
        # None = modo AO VIVO: a janela acompanha a amostra mais recente, como
        # sempre foi. Um valor = instante absoluto da borda DIREITA da janela,
        # fixado pelo usuario na barra de rolagem; a vista fica ancorada ali e
        # nao anda mais com as amostras que chegam, o que permite escolher com
        # calma uma regiao de interesse (e exporta-la) enquanto o ensaio segue.
        # E um instante absoluto, e nao uma distancia ate o "agora", justamente
        # para nao escorregar a cada amostra nova.
        self.ancora_t = None
        # Avisado a cada `redesenha` com (inicio, fim) em fracao do historico,
        # no formato que `ttk.Scrollbar.set` espera.
        self.on_rolagem = None

        # Pausa: "congela" a visualizacao guardando uma copia das series no
        # instante da pausa; `acrescenta()` continua alimentando `self.series`
        # normalmente (a aquisicao nao para), so o desenho passa a ler da
        # copia ate a visualizacao ser retomada.
        self.pausado = False
        self._series_pausadas = None
        self._altura_pausada = None
        self._referencia_pausada = None

        # Selecao de uma janela de tempo a arrasto do mouse (usada pelo
        # botao "exportar dados"); `_geom` guarda a ultima geometria de
        # desenho para converter posicao do mouse (px) em instante (t).
        self.selecionando = False
        self._callback_selecao = None
        self._sel_inicio = None
        self._sel_atual = None
        self._geom = None
        # Posicao (px) do mouse durante a selecao, para desenhar a linha
        # vertical de crosshair com o instante (t = ...) sob o cursor.
        self._hover_x = None

        self.bind('<Configure>', lambda _e: self.redesenha())
        self.bind('<ButtonPress-1>', self._sel_pressiona)
        self.bind('<B1-Motion>', self._sel_arrasta)
        self.bind('<ButtonRelease-1>', self._sel_solta)
        self.bind('<Motion>', self._sel_move)
        self.bind('<Leave>', self._sel_saida)

    def define_janela(self, janela_s):
        self.janela_s = float(janela_s)
        self._descarta_velhos()
        self.redesenha()

    def define_visivel(self, chave, visivel):
        self.visiveis[chave] = visivel
        self.redesenha()

    def alterna_modo(self):
        self.modo = 'dispersao' if self.modo == 'linha' else 'linha'
        self.redesenha()

    def alterna_escala(self):
        self.escala_adaptativa = not self.escala_adaptativa
        self.redesenha()

    def define_visivel_altura(self, visivel):
        self.altura_visivel = visivel
        self.redesenha()

    def define_ctrl_ativo(self, ativo):
        self.ctrl_ativo = ativo
        self.redesenha()

    def mostra_erro(self):
        return self.ctrl_ativo and self.visiveis.get(SERIE_ERRO[0], True)

    def series_em_uso(self):
        return SERIES_GRAFICO + (SERIES_CTRL if self.ctrl_ativo else ())

    def limpa_ctrl(self):
        """So as curvas do controlador (SERIES_CTRL e o erro), chamado por
        `AbaAula4` ao iniciar uma nova malha."""
        for chave, _r, _c in SERIES_CTRL + (SERIE_ERRO,):
            self.series[chave].clear()
            if self._series_pausadas is not None:
                self._series_pausadas[chave] = []

    def extensao(self):
        """(t_ini, t_fim) de TODO o historico em memoria, ou None se vazio."""
        fontes = [f for f in (self._fonte(c) for c, _r, _co in SERIES_GRAFICO) if f]
        if not fontes:
            return None
        return min(f[0][0] for f in fontes), max(f[-1][0] for f in fontes)

    def janela_visivel(self):
        """(t_ini, t_fim) da faixa de tempo efetivamente desenhada agora.

        Respeita a ancora de rolagem, mas sempre dentro do que existe em
        memoria: se o historico ja e menor que a janela escolhida, ou se as
        amostras ancoradas ja foram descartadas por idade, a vista volta
        sozinha para o trecho mais recente disponivel.
        """
        ext = self.extensao()
        if ext is None:
            return 0.0, self.janela_s
        t0, t1 = ext
        if self.ancora_t is None:
            t_fim = t1
        else:
            t_fim = min(t1, max(t0 + self.janela_s, self.ancora_t))
        return t_fim - self.janela_s, t_fim

    def fracoes_rolagem(self):
        """(inicio, fim) da janela visivel como fracao do historico, para a
        barra de rolagem. Historico menor que a janela: barra cheia."""
        ext = self.extensao()
        if ext is None:
            return 0.0, 1.0
        t0, t1 = ext
        span = t1 - t0
        if span <= self.janela_s:
            return 0.0, 1.0
        t_ini, t_fim = self.janela_visivel()
        return max(0.0, (t_ini - t0) / span), min(1.0, (t_fim - t0) / span)

    def rola_para(self, fracao_inicio):
        """Coloca a borda ESQUERDA da janela na fracao dada do historico.

        Encostar na direita solta a ancora e devolve o grafico ao modo ao
        vivo - sem isso nao haveria como voltar a acompanhar o ensaio depois
        de ter rolado para tras.
        """
        ext = self.extensao()
        if ext is None:
            return
        t0, t1 = ext
        span = t1 - t0
        if span <= self.janela_s:
            self.ancora_t = None
        else:
            t_fim = t0 + max(0.0, min(1.0, fracao_inicio)) * span + self.janela_s
            self.ancora_t = None if t_fim >= t1 - 1e-6 else max(t0 + self.janela_s, t_fim)
        self.redesenha()

    def rola_passos(self, passos, fracao_do_passo):
        """Desloca a janela em `passos` * `fracao_do_passo` da largura dela
        (usado pelas setas e pelo clique no vao da barra de rolagem)."""
        ext = self.extensao()
        if ext is None:
            return
        t0, t1 = ext
        span = t1 - t0
        if span <= self.janela_s:
            return
        _t_ini, t_fim = self.janela_visivel()
        self.rola_para((t_fim + passos * fracao_do_passo * self.janela_s
                        - self.janela_s - t0) / span)

    def ao_vivo(self):
        ext = self.extensao()
        if ext is None or self.ancora_t is None:
            return True
        return self.janela_visivel()[1] >= ext[1] - 1e-6

    def volta_ao_vivo(self):
        self.ancora_t = None
        self.redesenha()

    def _descarta_velhos(self):
        limite = max(self.janela_s, JANELA_MAX_S)
        for chave in self.series:
            pontos = self.series[chave]
            if not pontos:
                continue
            t = pontos[-1][0]
            while pontos and t - pontos[0][0] > limite:
                pontos.popleft()
        if self.serie_altura:
            t = self.serie_altura[-1][0]
            while self.serie_altura and t - self.serie_altura[0][0] > limite:
                self.serie_altura.popleft()
        if self.serie_referencia:
            t = self.serie_referencia[-1][0]
            while self.serie_referencia and t - self.serie_referencia[0][0] > limite:
                self.serie_referencia.popleft()

    def acrescenta(self, t, valores_pct):
        for chave, pct in valores_pct.items():
            if chave in self.series:
                self.series[chave].append((t, pct))
        self._descarta_velhos()

    def acrescenta_altura(self, t, h_mm):
        self.serie_altura.append((t, h_mm))
        self._descarta_velhos()

    def acrescenta_referencia(self, t, r_mm):
        """Um ponto da referencia r[k] da malha fechada da Aula 4 (AbaAula4)."""
        self.serie_referencia.append((t, r_mm))
        self._descarta_velhos()

    def limpa_referencia(self):
        """So a curva de referencia, chamado por `AbaAula4` ao iniciar uma
        nova malha - as demais curvas (h, PUMP2, ...) do ensaio anterior
        continuam no grafico, como em qualquer outra aba."""
        self.serie_referencia.clear()
        if self._referencia_pausada is not None:
            self._referencia_pausada = []
        self.redesenha()

    def limpa(self):
        for pontos in self.series.values():
            pontos.clear()
        self.serie_altura.clear()
        self.serie_referencia.clear()
        if self._series_pausadas is not None:
            self._series_pausadas = {chave: [] for chave in self.series}
        if self._altura_pausada is not None:
            self._altura_pausada = []
        if self._referencia_pausada is not None:
            self._referencia_pausada = []
        self.ancora_t = None
        self.redesenha()

    # -- pausa da visualizacao ------------------------------------------

    def pausa(self):
        self.pausado = True
        self._series_pausadas = {chave: list(pontos) for chave, pontos in self.series.items()}
        self._altura_pausada = list(self.serie_altura)
        self._referencia_pausada = list(self.serie_referencia)

    def retoma(self):
        self.pausado = False
        self._series_pausadas = None
        self._altura_pausada = None
        self._referencia_pausada = None
        self.redesenha()

    def _fonte(self, chave):
        if self.pausado and self._series_pausadas is not None:
            return self._series_pausadas.get(chave, [])
        return self.series[chave]

    def _visiveis(self, chave):
        t_ini, t_fim = self.janela_visivel()
        return [(t, v) for t, v in self._fonte(chave) if t_ini <= t <= t_fim]

    def _fonte_altura(self):
        if self.pausado and self._altura_pausada is not None:
            return self._altura_pausada
        return self.serie_altura

    def _visiveis_altura(self):
        t_ini, t_fim = self.janela_visivel()
        return [(t, v) for t, v in self._fonte_altura() if t_ini <= t <= t_fim]

    def _fonte_referencia(self):
        if self.pausado and self._referencia_pausada is not None:
            return self._referencia_pausada
        return self.serie_referencia

    def _visiveis_referencia(self):
        t_ini, t_fim = self.janela_visivel()
        return [(t, v) for t, v in self._fonte_referencia() if t_ini <= t <= t_fim]

    # -- selecao de janela de tempo (para exportar dados) -----------------

    def ativa_selecao(self, callback):
        self.selecionando = True
        self._callback_selecao = callback
        self._sel_inicio = None
        self._sel_atual = None
        self.configure(cursor='crosshair')

    def desativa_selecao(self):
        self.selecionando = False
        self._callback_selecao = None
        self._sel_inicio = None
        self._sel_atual = None
        self._hover_x = None
        self.configure(cursor='')
        self.redesenha()

    def _px_para_t(self, x):
        if self._geom is None:
            return 0.0
        x0, _y0, x1, _y1, t_ini, t_fim = self._geom
        x = max(x0, min(x1, x))
        frac = (x - x0) / (x1 - x0) if x1 > x0 else 0.0
        return t_ini + frac * (t_fim - t_ini)

    def _sel_pressiona(self, evento):
        if not self.selecionando or self._geom is None:
            return
        self._sel_inicio = evento.x
        self._sel_atual = evento.x

    def _sel_arrasta(self, evento):
        if not self.selecionando or self._sel_inicio is None:
            return
        self._sel_atual = evento.x
        self.redesenha()

    def _sel_move(self, evento):
        if not self.selecionando or self._geom is None:
            return
        self._hover_x = evento.x
        self.redesenha()

    def _sel_saida(self, _evento):
        if self._hover_x is None:
            return
        self._hover_x = None
        self.redesenha()

    def _sel_solta(self, _evento):
        if not self.selecionando or self._sel_inicio is None:
            return
        xa, xb = sorted((self._sel_inicio, self._sel_atual))
        self._sel_inicio = None
        self._sel_atual = None
        if xb - xa < 4:
            self.redesenha()
            return
        t_ini, t_fim = self._px_para_t(xa), self._px_para_t(xb)
        callback = self._callback_selecao
        self.desativa_selecao()
        if callback is not None:
            callback(t_ini, t_fim)

    def redesenha(self):
        self.delete('all')
        esq, topo, dir_, base = self.MARGEM
        larg, alt = self.winfo_width(), self.winfo_height()
        x0, y0 = esq, topo
        x1, y1 = larg - dir_, alt - base
        if x1 - x0 < 40 or y1 - y0 < 40:
            return

        if self.on_rolagem is not None:
            self.on_rolagem(*self.fracoes_rolagem())

        series_em_uso = self.series_em_uso()
        todos_pontos = {chave: self._visiveis(chave) for chave, _r, _c in series_em_uso}
        t_ini, t_fim = self.janela_visivel()
        t_ini = max(0.0, t_ini)
        if t_fim - t_ini < 1.0:
            t_fim = t_ini + 1.0
        if self.escala_adaptativa:
            # so as series marcadas em "mostrar" entram na conta: uma serie
            # escondida nao esta "em exibicao" e nao deve esticar o eixo
            valores_pct = [v for chave, _r, _c in series_em_uso
                           if self.visiveis.get(chave, True)
                           for _t, v in todos_pontos[chave]]
            v_lo, v_hi = faixa_ajustada(valores_pct, span_minimo=1.0,
                                        reserva=(-5.0, 105.0))
        else:
            v_lo, v_hi = -5.0, 105.0
            # u_K pode ser negativo e u_Ti passar de 100 %: estende o eixo
            # (em multiplos de 20) so o bastante para caberem, sem cortar.
            ctrl = [v for chave, _r, _c in SERIES_CTRL
                    if self.ctrl_ativo and self.visiveis.get(chave, True)
                    for _t, v in todos_pontos[chave]]
            if ctrl:
                v_lo = min(v_lo, math.floor(min(ctrl) / 20.0) * 20.0 - 5.0)
                v_hi = max(v_hi, math.ceil(max(ctrl) / 20.0) * 20.0 + 5.0)
            if self.mostra_erro():
                # o fundo do eixo de h e -ALTURA_MAX_MM*f/(1-f), com f =
                # -v_lo/(v_hi-v_lo) (ver `faixa_altura`); para caber um erro
                # de -m mm, basta v_lo = -m/ALTURA_MAX_MM * v_hi.
                e_min = min((v for _t, v in self._visiveis(SERIE_ERRO[0])), default=0.0)
                if e_min < 0:
                    v_lo = min(v_lo, -(-e_min / ALTURA_MAX_MM) * v_hi - 5.0)

        def px(t):
            return x0 + (t - t_ini) / (t_fim - t_ini) * (x1 - x0)

        def py(v):
            return y1 - (v - v_lo) / (v_hi - v_lo) * (y1 - y0)

        if self.escala_adaptativa:
            marcas_pct = [(v_lo + (v_hi - v_lo) * i / 4, '.1f') for i in range(5)]
        else:
            passo_marca = 20
            primeira = math.ceil(v_lo / passo_marca) * passo_marca
            marcas_pct = [(pct, '.0f') for pct in range(int(primeira), int(v_hi) + 1, passo_marca)]
        for pct, formato in marcas_pct:
            y = py(pct)
            self.create_line(x0, y, x1, y, fill='#e8e8e8')
            self.create_text(x0 - 6, y, text=f'{pct:{formato}}%', anchor='e',
                             font=('TkDefaultFont', 8), fill='#555')
        for i in range(5):
            t = t_ini + (t_fim - t_ini) * i / 4
            x = px(t)
            self.create_line(x, y0, x, y1, fill='#f2f2f2')
            self.create_text(x, y1 + 6, text=rotulo_tempo(t), anchor='n',
                             font=('TkDefaultFont', 8), fill='#555')
        self.create_rectangle(x0, y0, x1, y1, outline='#909090')

        # A legenda so entra para series visiveis, e a largura de cada item e
        # medida de verdade (fonte proporcional) em vez de estimada por
        # len(rotulo) - a estimativa antiga subestimava rotulos como "VALVE
        # (valvula S)" e fazia um item invadir o proximo.
        legenda_x = x0
        for chave, rotulo, cor in series_em_uso:
            if not self.visiveis.get(chave, True):
                continue
            pontos = todos_pontos[chave]
            if self.modo == 'dispersao':
                if len(pontos) >= 2:
                    traco = [px(pontos[0][0]), py(max(v_lo, min(v_hi, pontos[0][1])))]
                    for i in range(1, len(pontos)):
                        t_ant, v_ant = pontos[i - 1]
                        t_atu, v_atu = pontos[i]
                        x_atu = px(t_atu)
                        y_ant = py(max(v_lo, min(v_hi, v_ant)))
                        y_atu = py(max(v_lo, min(v_hi, v_atu)))
                        traco += [x_atu, y_ant, x_atu, y_atu]
                    # tracejada e fina para nao competir com os pontos, que
                    # sao a leitura de fato (stipple foi trocado por dash:
                    # o suporte a stipple em linhas e inconsistente entre
                    # backends do Tk, deixando o canvas em branco).
                    self.create_line(*traco, fill=cor, width=1, dash=(3, 2))
                raio = 5.5
                for t, v in pontos:
                    y = py(max(v_lo, min(v_hi, v)))
                    x = px(t)
                    self.create_oval(x - raio, y - raio, x + raio, y + raio,
                                     fill=cor, outline='')
            else:
                if len(pontos) >= 2:
                    traco = []
                    for t, v in pontos:
                        traco += [px(t), py(max(v_lo, min(v_hi, v)))]
                    self.create_line(*traco, fill=cor, width=2)
                if pontos:
                    ultimo = pontos[-1][1]
                    self.create_oval(px(pontos[-1][0]) - 3, py(max(v_lo, min(v_hi, ultimo))) - 3,
                                     px(pontos[-1][0]) + 3, py(max(v_lo, min(v_hi, ultimo))) + 3,
                                     fill=cor, outline='')
            self.create_rectangle(legenda_x, 8, legenda_x + 10, 18, fill=cor, outline='')
            self.create_text(legenda_x + 14, 13, text=rotulo, anchor='w',
                             font=self._fonte_legenda, fill='#333')
            legenda_x += 14 + self._fonte_legenda.measure(rotulo) + 20

        # Curva de altura (h, mm): eixo vertical proprio, a direita, em faixa
        # FIXA de 0 a ALTURA_MAX_MM (nao 0-100 %, como as demais series, nem
        # auto-ajustada aos pontos visiveis - ver `faixa_altura`).
        mostra_h = self.altura_disponivel and self.altura_visivel
        pontos_erro = self._visiveis(SERIE_ERRO[0]) if self.mostra_erro() else []
        if mostra_h or pontos_erro:
            pontos_alt = self._visiveis_altura() if mostra_h else []
            if self.escala_adaptativa:
                alt_lo, alt_hi = faixa_ajustada(
                    [v for _t, v in pontos_alt + pontos_erro], span_minimo=5.0,
                    reserva=faixa_altura(-5.0, 105.0))
            else:
                alt_lo, alt_hi = faixa_altura(v_lo, v_hi)

            def py_alt(v):
                # o clamp mantem dentro da moldura uma leitura fora da faixa
                # (calibracao trocada, LT saturado), em vez de riscar por cima
                # da legenda e do eixo do tempo
                v = max(alt_lo, min(alt_hi, v))
                return y1 - (v - alt_lo) / (alt_hi - alt_lo) * (y1 - y0)

            if self.escala_adaptativa:
                marcas_alt = [(alt_lo + (alt_hi - alt_lo) * i / 4, '.1f') for i in range(5)]
            else:
                # com o topo fixo, os rotulos podem ser redondos (0, 50, ... 250)
                # em vez das fracoes do maximo em tela (0, 62, 125, 188, 250)
                marcas_alt = []
                v = math.ceil(alt_lo / ALTURA_PASSO_MM - 1e-9) * ALTURA_PASSO_MM
                while v <= alt_hi + 1e-9:
                    marcas_alt.append((v, '.0f'))
                    v += ALTURA_PASSO_MM
            for v, formato in marcas_alt:
                self.create_text(x1 + 6, py_alt(v), text=f'{v:{formato}}', anchor='w',
                                 font=('TkDefaultFont', 8), fill=COR_ALTURA)
            self.create_text(x1 + 6, y0 - 10, text='h, e [mm]' if pontos_erro else 'h [mm]',
                             anchor='w',
                             font=('TkDefaultFont', 8, 'italic'), fill=COR_ALTURA)

            if self.modo == 'dispersao':
                if len(pontos_alt) >= 2:
                    traco = [px(pontos_alt[0][0]), py_alt(pontos_alt[0][1])]
                    for i in range(1, len(pontos_alt)):
                        _t_ant, v_ant = pontos_alt[i - 1]
                        t_atu, v_atu = pontos_alt[i]
                        x_atu = px(t_atu)
                        traco += [x_atu, py_alt(v_ant), x_atu, py_alt(v_atu)]
                    self.create_line(*traco, fill=COR_ALTURA, width=1, dash=(3, 2))
                raio = 5.5
                for t, v in pontos_alt:
                    x, y = px(t), py_alt(v)
                    self.create_oval(x - raio, y - raio, x + raio, y + raio,
                                     fill=COR_ALTURA, outline='')
            else:
                if len(pontos_alt) >= 2:
                    traco = []
                    for t, v in pontos_alt:
                        traco += [px(t), py_alt(v)]
                    self.create_line(*traco, fill=COR_ALTURA, width=2)
                if pontos_alt:
                    t_ult, v_ult = pontos_alt[-1]
                    x, y = px(t_ult), py_alt(v_ult)
                    self.create_oval(x - 3, y - 3, x + 3, y + 3, fill=COR_ALTURA, outline='')

            if mostra_h:
                self.create_rectangle(legenda_x, 8, legenda_x + 10, 18, fill=COR_ALTURA,
                                      outline='')
                self.create_text(legenda_x + 14, 13, text=ROTULO_ALTURA, anchor='w',
                                 font=self._fonte_legenda, fill='#333')
                legenda_x += 14 + self._fonte_legenda.measure(ROTULO_ALTURA) + 20

            # Erro e = r - h (mm): mesmo eixo da altura; linha de zero tracejada
            # para ver de que lado da referencia o nivel esta.
            if pontos_erro:
                self.create_line(x0, py_alt(0.0), x1, py_alt(0.0), fill=SERIE_ERRO[2],
                                 dash=(2, 4))
                if self.modo == 'dispersao':
                    if len(pontos_erro) >= 2:
                        traco = [px(pontos_erro[0][0]), py_alt(pontos_erro[0][1])]
                        for i in range(1, len(pontos_erro)):
                            x_atu = px(pontos_erro[i][0])
                            traco += [x_atu, py_alt(pontos_erro[i - 1][1]),
                                      x_atu, py_alt(pontos_erro[i][1])]
                        self.create_line(*traco, fill=SERIE_ERRO[2], width=1, dash=(3, 2))
                    for t, v in pontos_erro:
                        x, y = px(t), py_alt(v)
                        self.create_oval(x - 5.5, y - 5.5, x + 5.5, y + 5.5,
                                         fill=SERIE_ERRO[2], outline='')
                elif len(pontos_erro) >= 2:
                    traco = []
                    for t, v in pontos_erro:
                        traco += [px(t), py_alt(v)]
                    self.create_line(*traco, fill=SERIE_ERRO[2], width=2)
                self.create_rectangle(legenda_x, 8, legenda_x + 10, 18, fill=SERIE_ERRO[2],
                                      outline='')
                self.create_text(legenda_x + 14, 13, text=SERIE_ERRO[1], anchor='w',
                                 font=self._fonte_legenda, fill='#333')
                legenda_x += 14 + self._fonte_legenda.measure(SERIE_ERRO[1]) + 20

            # Curva de referencia (r, mm) da malha fechada da Aula 4: mesmo
            # eixo (py_alt) da curva de altura acima, sempre desenhada como
            # degrau (a referencia e constante dentro de cada patamar) e
            # independente do modo linha/dispersao escolhido para as demais
            # series - interpolar retas entre dois patamares diferentes
            # sugeriria uma rampa que a referencia nunca fez.
            if self.serie_referencia:
                pontos_ref = self._visiveis_referencia()
                if len(pontos_ref) >= 2:
                    traco = [px(pontos_ref[0][0]), py_alt(pontos_ref[0][1])]
                    for i in range(1, len(pontos_ref)):
                        _t_ant, v_ant = pontos_ref[i - 1]
                        t_atu, v_atu = pontos_ref[i]
                        x_atu = px(t_atu)
                        traco += [x_atu, py_alt(v_ant), x_atu, py_alt(v_atu)]
                    self.create_line(*traco, fill=COR_REFERENCIA, width=1.5, dash=(6, 3))
                elif pontos_ref:
                    x, y = px(pontos_ref[0][0]), py_alt(pontos_ref[0][1])
                    self.create_line(x - 5, y, x + 5, y, fill=COR_REFERENCIA, width=1.5,
                                     dash=(6, 3))
                self.create_rectangle(legenda_x, 8, legenda_x + 10, 18, fill=COR_REFERENCIA,
                                      outline='')
                self.create_text(legenda_x + 14, 13, text=ROTULO_REFERENCIA, anchor='w',
                                 font=self._fonte_legenda, fill='#333')
                legenda_x += 14 + self._fonte_legenda.measure(ROTULO_REFERENCIA) + 20

        self._geom = (x0, y0, x1, y1, t_ini, t_fim)

        if self.selecionando and self._sel_inicio is not None and self._sel_atual is not None:
            xa = max(x0, min(x1, self._sel_inicio))
            xb = max(x0, min(x1, self._sel_atual))
            self.create_rectangle(xa, y0, xb, y1, fill='#3a7bd5', outline='#2a5aa0',
                                  stipple='gray25')

        # Crosshair da selecao. Dois casos:
        # - arrasto em andamento: uma linha em cada extremo (inicio fixo,
        #   fim acompanhando o mouse) com uma seta e o Delta t da janela
        #   atual no meio, para o usuario ver o tamanho do recorte antes de
        #   soltar o botao;
        # - so passeando o mouse (antes de comecar o arrasto): uma unica
        #   linha com o instante (t = ...) sob o cursor, para mirar o ponto
        #   de corte.
        if self.selecionando and self._sel_inicio is not None and self._sel_atual is not None:
            xe = max(x0, min(x1, self._sel_inicio))
            xd = max(x0, min(x1, self._sel_atual))
            xa, xb = min(xe, xd), max(xe, xd)
            # width=2 (nao o padrao 1px) e tag_raise explicito: coincidindo
            # em x com a borda do retangulo de selecao, uma linha de 1px
            # pode ficar por baixo dela dependendo do backend do Tk (visto
            # no Aqua/macOS) mesmo tendo sido criada depois - forcar o topo
            # da pilha resolve independente da plataforma.
            id_la = self.create_line(xa, y0, xa, y1, fill='#c0392b', width=2, dash=(4, 2))
            id_lb = self.create_line(xb, y0, xb, y1, fill='#c0392b', width=2, dash=(4, 2))
            self.tag_raise(id_la)
            self.tag_raise(id_lb)
            delta_t = abs(self._px_para_t(xb) - self._px_para_t(xa))
            y_seta = (y0 + y1) / 2
            if xb - xa > 24:
                self.create_line(xa + 4, y_seta, xb - 4, y_seta, fill='#c0392b',
                                 width=2, arrow='both', arrowshape=(8, 10, 4))
            xm = (xa + xb) / 2
            id_texto = self.create_text(
                xm, y_seta - 8, text=f'Δt = {rotulo_tempo(delta_t)}', anchor='s',
                font=('TkDefaultFont', 9, 'bold'), fill='#c0392b')
            caixa = self.bbox(id_texto)
            if caixa is not None:
                pad = 3
                id_fundo = self.create_rectangle(
                    caixa[0] - pad, caixa[1] - pad, caixa[2] + pad, caixa[3] + pad,
                    fill='white', outline='#c0392b')
                self.tag_raise(id_texto, id_fundo)
        elif self.selecionando and self._hover_x is not None:
            xh = max(x0, min(x1, self._hover_x))
            self.create_line(xh, y0, xh, y1, fill='#c0392b', dash=(4, 2))
            t_h = self._px_para_t(xh)
            xt = max(x0 + 32, min(x1 - 32, xh))
            id_texto = self.create_text(
                xt, y0 + 4, text=f't = {rotulo_tempo(t_h)}', anchor='n',
                font=('TkDefaultFont', 9, 'bold'), fill='#c0392b')
            caixa = self.bbox(id_texto)
            if caixa is not None:
                pad = 3
                id_fundo = self.create_rectangle(
                    caixa[0] - pad, caixa[1] - pad, caixa[2] + pad, caixa[3] + pad,
                    fill='white', outline='#c0392b')
                self.tag_raise(id_texto, id_fundo)


class QuadroRolavel(ttk.Frame):
    """Frame com barra de rolagem vertical.

    Usado para o conteudo de cada aba: quando os controles de uma aula
    ocupam mais altura do que cabe na janela, o conteudo rola em vez de
    ficar cortado ou forcar a janela a crescer.
    """

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.scroll = ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        self.interior = ttk.Frame(self.canvas)

        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.pack(side='left', fill='both', expand=True)
        self.scroll.pack(side='right', fill='y')

        self._janela = self.canvas.create_window((0, 0), window=self.interior, anchor='nw')
        self.interior.bind('<Configure>', self._atualiza_regiao_rolagem)
        self.canvas.bind('<Configure>', self._ajusta_largura_interior)
        self.canvas.bind('<Enter>', lambda _e: self._liga_roda())
        self.canvas.bind('<Leave>', lambda _e: self._desliga_roda())

    def _atualiza_regiao_rolagem(self, _evento=None):
        self.canvas.configure(scrollregion=self.canvas.bbox('all'))

    def _ajusta_largura_interior(self, evento):
        self.canvas.itemconfigure(self._janela, width=evento.width)

    def _liga_roda(self):
        self.canvas.bind_all('<MouseWheel>', self._roda)
        self.canvas.bind_all('<Button-4>', self._roda)
        self.canvas.bind_all('<Button-5>', self._roda)

    def _desliga_roda(self):
        self.canvas.unbind_all('<MouseWheel>')
        self.canvas.unbind_all('<Button-4>')
        self.canvas.unbind_all('<Button-5>')

    def _roda(self, evento):
        if evento.num == 4:
            self.canvas.yview_scroll(-1, 'units')
        elif evento.num == 5:
            self.canvas.yview_scroll(1, 'units')
        else:
            self.canvas.yview_scroll(-1 if evento.delta > 0 else 1, 'units')


# ---------------------------------------------------------------------------
# Abas das aulas
# ---------------------------------------------------------------------------

class AbaBase(ttk.Frame):
    """Interface comum que o hub espera de toda aba de aula."""

    def __init__(self, master, app):
        super().__init__(master, padding=10)
        self.app = app

    def atualiza_amostra(self, t, valores):
        """Chamado a cada amostra nova (na thread principal do Tk). Opcional."""


class PainelLeituras(ttk.LabelFrame):
    """Leitura de todas as tags (Tab. 1.3), sempre visivel ao lado do grafico.

    Antes vivia dentro da aba da Aula 1; virou um painel proprio porque e
    util em qualquer aula, nao so na primeira - por isso mora fora do
    `ttk.Notebook`, ao lado do grafico, e nao dentro de uma aba.

    Tambem concentra o botao "Ajustar calibracao de LT": o assistente da aba
    da Aula 1 calcula os coeficientes (grau + polinomio, Secao 1.1.2.1), mas
    e aqui que eles sao colados para virarem uma calibracao ativa na sessao,
    usada tanto para converter a leitura de LT desta tabela quanto para
    plotar e exportar a curva de altura no grafico ao vivo (ver `Janela`).
    """

    def __init__(self, master, on_calibracao=None, **kw):
        super().__init__(master, text='Leitura de todas as tags (Tab. 1.3)', **kw)
        self.ultimas = {}   # {chave: conta}, atualizado a cada amostra
        self._rotulos = {}
        self._on_calibracao = on_calibracao
        self._calib_lt = None    # None = usa contas_para_altura da biblioteca
        self._grau_lt = None

        cabecalhos = ('tag', 'contas', 'volts', '')
        for j, texto in enumerate(cabecalhos):
            ttk.Label(self, text=texto, font=('TkDefaultFont', 8, 'bold')).grid(
                row=0, column=j, sticky='w', padx=(0, 14))
        for i, chave in enumerate(('LT', 'FT2', 'PT', 'TT5', 'PUMP2', 'VALVE'), start=1):
            ttk.Label(self, text=chave).grid(row=i, column=0, sticky='w', padx=(0, 14))
            rot_contas = ttk.Label(self, text='--')
            rot_contas.grid(row=i, column=1, sticky='w', padx=(0, 14))
            rot_volts = ttk.Label(self, text='--')
            rot_volts.grid(row=i, column=2, sticky='w', padx=(0, 14))
            rot_extra = ttk.Label(self, text='')
            rot_extra.grid(row=i, column=3, sticky='w')
            self._rotulos[chave] = (rot_contas, rot_volts, rot_extra)

        linha_calib = i + 1
        ttk.Button(self, text='Ajustar calibração de LT',
                  command=self._abre_dialogo_calibracao).grid(
            row=linha_calib, column=0, columnspan=4, sticky='w', pady=(10, 2))
        self.lb_calib = ttk.Label(
            self, text='calibração de LT: biblioteca (conversoes.py)',
            font=('TkDefaultFont', 8), foreground='#555')
        self.lb_calib.grid(row=linha_calib + 1, column=0, columnspan=4, sticky='w')

    def contas_para_altura_ativa(self, conta):
        """h(conta) pela calibracao definida na sessao, ou pela biblioteca
        (`comum/conversoes.py`) se nenhuma tiver sido definida ainda.

        E ESTA a conversao usada em todo lugar que o hub escreve um h em mm:
        a tabela de leituras, a curva de altura do grafico, o CSV/PDF da
        exportacao por recorte e os CSV gravados pelas abas das Aulas 2 e 3.
        Um unico ponto de conversao evita o pior erro possivel aqui - um
        ensaio gravado com coeficientes diferentes dos que o aluno esta vendo
        na tela.
        """
        if self._calib_lt is not None:
            return _avalia_polinomio(self._calib_lt, conta)
        return contas_para_altura(conta)

    def calibracao_da_sessao(self):
        """(grau, coefs) da calibracao colada nesta sessao, ou None se o hub
        ainda esta usando os coeficientes da biblioteca."""
        if self._calib_lt is None:
            return None
        return self._grau_lt, self._calib_lt

    def rotulo_calibracao(self):
        if self._calib_lt is None:
            return 'biblioteca (conversoes.py)'
        return f'definida na sessao (grau {self._grau_lt})'

    def _abre_dialogo_calibracao(self):
        janela = tk.Toplevel(self)
        janela.title('Ajustar calibração de LT')
        janela.resizable(False, False)
        janela.transient(self.winfo_toplevel())

        ttk.Label(
            janela,
            text='Escolha o grau do polinômio h(N) ajustado na Seção 1.1.2.1\n'
                 '(N = leitura de LT em contas) e cole os coeficientes calculados\n'
                 'pelo assistente "ajustar (graus 1-3)" da aba Aula 1, do maior\n'
                 'grau para o menor - a mesma ordem de LT_COEFS_H_DE_CONTAS.',
            justify='left').grid(row=0, column=0, columnspan=2, sticky='w',
                                 padx=10, pady=(10, 8))

        if self._calib_lt is not None:
            origem_atual = f'definida na sessão (grau {self._grau_lt})'
            equacao_atual = _formata_equacao_polinomio(self._calib_lt)
        else:
            origem_atual = 'biblioteca (conversoes.py)'
            equacao_atual = _formata_equacao_polinomio(LT_COEFS_H_DE_CONTAS)
        quadro_atual = ttk.LabelFrame(janela, text='Calibração em vigor agora')
        quadro_atual.grid(row=1, column=0, columnspan=2, sticky='we',
                          padx=10, pady=(0, 8))
        ttk.Label(quadro_atual, text=f'Origem: {origem_atual}').pack(
            anchor='w', padx=8, pady=(4, 0))
        ttk.Label(quadro_atual, text=equacao_atual, font=('TkFixedFont', 9),
                  wraplength=460, justify='left').pack(
            anchor='w', padx=8, pady=(2, 6))

        var_grau = tk.IntVar(value=self._grau_lt or 3)
        linha_grau = ttk.Frame(janela)
        linha_grau.grid(row=2, column=0, columnspan=2, sticky='w', padx=10)
        for grau in (1, 2, 3):
            ttk.Radiobutton(
                linha_grau, text=f'grau {grau}', variable=var_grau, value=grau,
                command=lambda: _monta_campos_coefs()).pack(side='left', padx=(0, 12))

        quadro_coefs = ttk.Frame(janela)
        quadro_coefs.grid(row=3, column=0, columnspan=2, sticky='w', padx=10, pady=(8, 0))
        vars_coefs = []

        def _monta_campos_coefs():
            for filho in quadro_coefs.winfo_children():
                filho.destroy()
            vars_coefs.clear()
            grau = var_grau.get()
            coefs_atuais = self._calib_lt if (self._calib_lt and self._grau_lt == grau) else None
            for i in range(grau + 1):
                expoente = grau - i
                rotulo = f'a{expoente} (N^{expoente}):' if expoente else 'a0 (termo constante):'
                ttk.Label(quadro_coefs, text=rotulo).grid(row=i, column=0, sticky='w', pady=1)
                valor_inicial = repr(coefs_atuais[i]) if coefs_atuais else ''
                var = tk.StringVar(value=valor_inicial)
                ttk.Entry(quadro_coefs, textvariable=var, width=22).grid(
                    row=i, column=1, sticky='w', padx=(6, 0), pady=1)
                vars_coefs.append(var)

        _monta_campos_coefs()

        def _confirma():
            grau = var_grau.get()
            try:
                coefs = tuple(float(v.get()) for v in vars_coefs)
            except ValueError:
                messagebox.showerror(
                    'Coeficiente inválido', 'Todos os coeficientes devem ser números '
                    '(use ponto decimal, como na saída do assistente de calibração).',
                    parent=janela)
                return
            self._calib_lt = coefs
            self._grau_lt = grau
            self.lb_calib.configure(
                text=f'calibração de LT: definida na sessão (grau {grau})')
            if self._on_calibracao is not None:
                self._on_calibracao(grau, coefs)
            janela.destroy()

        def _restaura_biblioteca():
            self._calib_lt = None
            self._grau_lt = None
            self.lb_calib.configure(text='calibração de LT: biblioteca (conversoes.py)')
            if self._on_calibracao is not None:
                self._on_calibracao(None, None)
            janela.destroy()

        botoes = ttk.Frame(janela)
        botoes.grid(row=4, column=0, columnspan=2, sticky='e', padx=10, pady=10)
        ttk.Button(botoes, text='usar biblioteca (padrão)',
                  command=_restaura_biblioteca).pack(side='left', padx=(0, 16))
        ttk.Button(botoes, text='cancelar', command=janela.destroy).pack(side='left')
        ttk.Button(botoes, text='OK', command=_confirma).pack(side='left', padx=(8, 0))

    def atualiza_amostra(self, _t, valores):
        self.ultimas = valores
        for chave, (rot_contas, rot_volts, rot_extra) in self._rotulos.items():
            conta = valores.get(chave)
            if conta is None:
                continue
            rot_contas.configure(text=f'{conta:6d}')
            rot_volts.configure(text=f'{conta_para_volts(conta):+6.3f} V')
            if chave == 'LT':
                rotulo_calib = f'grau {self._grau_lt}' if self._calib_lt is not None else 'biblioteca'
                rot_extra.configure(
                    text=f'{self.contas_para_altura_ativa(conta):7.1f} mm ({rotulo_calib})')
            elif chave == 'FT2':
                rot_extra.configure(text=f'{contas_para_vazao(conta):6.3f} L/min')
            elif chave in ('PUMP2', 'VALVE'):
                rot_extra.configure(text=f'{conta_para_percentual(conta):5.1f} % do comando')


class AbaAula1(AbaBase):
    """Aula 1 - calibracao de LT (Secao 1.3.4).

    A leitura de todas as tags (Tab. 1.3) mora em `PainelLeituras`, sempre
    visivel ao lado do grafico - nao aqui.
    """

    def __init__(self, master, app):
        super().__init__(master, app)
        self._pontos = []   # [(h_mm, contas), ...]
        self._coefs_por_grau = None   # preenchido por _ajusta; None invalida o grafico
        self._monta()

    def _monta(self):
        calib = ttk.LabelFrame(
            self, text='Calibracao de LT (Secao 1.3.4 - Tab. 1.6)', padding=10)
        calib.pack(fill='both', expand=True)
        ttk.Label(
            calib, text='Para cada altura da regua: ajuste o nivel, digite o h medido\n'
                        'em mm e clique "adicionar ponto" (captura a leitura atual de LT).',
            justify='left').grid(row=0, column=0, columnspan=6, sticky='w', pady=(0, 8))

        ttk.Label(calib, text='h medido (mm):').grid(row=1, column=0, sticky='w')
        self.var_h = tk.StringVar()
        ttk.Entry(calib, textvariable=self.var_h, width=10).grid(
            row=1, column=1, sticky='w', padx=(4, 14))
        ttk.Button(calib, text='adicionar ponto', command=self._adiciona_ponto).grid(
            row=1, column=2, sticky='w')
        ttk.Button(calib, text='remover selecionado', command=self._remove_ponto).grid(
            row=1, column=3, sticky='w', padx=(8, 0))
        ttk.Button(calib, text='limpar tabela', command=self._limpa_tabela).grid(
            row=1, column=4, sticky='w', padx=(8, 0))
        ttk.Button(calib, text='carregar calibracao_lt.csv',
                   command=self._carrega_csv).grid(row=1, column=5, sticky='w', padx=(8, 0))

        self.tabela = ttk.Treeview(
            calib, columns=('h', 'contas'), show='headings', height=8)
        self.tabela.heading('h', text='h [mm]')
        self.tabela.heading('contas', text='contas de LT_ADC')
        self.tabela.grid(row=2, column=0, columnspan=6, sticky='nsew', pady=8)
        calib.rowconfigure(2, weight=1)
        calib.columnconfigure(5, weight=1)

        botoes = ttk.Frame(calib)
        botoes.grid(row=3, column=0, columnspan=6, sticky='w')
        ttk.Button(botoes, text='salvar CSV (calibracao_lt.csv)',
                   command=self._salva_csv).pack(side='left')
        ttk.Button(botoes, text='ajustar (graus 1-3) e mostrar coeficientes',
                   command=self._ajusta).pack(side='left', padx=(8, 0))
        # So habilitado depois de um _ajusta bem-sucedido (ver _ajusta): o
        # grafico depende dos coeficientes calculados la, e fica invalido
        # (desabilitado de novo) assim que a tabela de pontos muda.
        self.bt_exportar_grafico = ttk.Button(
            botoes, text='exportar gráfico da calibração',
            command=self._exporta_grafico, state='disabled')
        self.bt_exportar_grafico.pack(side='left', padx=(8, 0))

        # `wrap='none'` (nao 'word'): a tabela de RMSE/erro max/R2 e as
        # equacoes usam espacamento fixo para alinhar colunas, e isso so
        # funciona se a linha nunca for quebrada ao meio - quebrar por
        # palavra (como no padrao antigo) desalinhava o cabecalho em
        # relacao aos valores sempre que o painel ficava mais estreito que a
        # linha mais longa. Uma barra horizontal cobre o que nao couber.
        quadro_resultado = ttk.Frame(calib)
        quadro_resultado.grid(row=4, column=0, columnspan=6, sticky='nsew', pady=(8, 0))
        quadro_resultado.rowconfigure(0, weight=1)
        quadro_resultado.columnconfigure(0, weight=1)
        calib.rowconfigure(4, weight=1)

        self.txt_resultado = tk.Text(quadro_resultado, height=8, wrap='none',
                                     font=('TkFixedFont', 9))
        barra_h = ttk.Scrollbar(
            quadro_resultado, orient='horizontal', command=self.txt_resultado.xview)
        self.txt_resultado.configure(xscrollcommand=barra_h.set)
        self.txt_resultado.grid(row=0, column=0, sticky='nsew')
        barra_h.grid(row=1, column=0, sticky='we')

    def _adiciona_ponto(self):
        lt_atual = self.app.painel_leituras.ultimas.get('LT')
        if lt_atual is None:
            messagebox.showwarning('Sem leitura', 'Ainda nao ha leitura de LT. Aguarde a conexao.')
            return
        try:
            h = float(self.var_h.get().replace(',', '.'))
        except ValueError:
            messagebox.showerror('h invalido', 'Digite o h medido na regua, em mm.')
            return
        self._pontos.append((h, lt_atual))
        self.tabela.insert('', 'end', values=(f'{h:.1f}', lt_atual))
        self.var_h.set('')
        self._invalida_ajuste()

    def _remove_ponto(self):
        for item in self.tabela.selection():
            idx = self.tabela.index(item)
            self.tabela.delete(item)
            del self._pontos[idx]
        self._invalida_ajuste()

    def _limpa_tabela(self):
        if not self._pontos:
            return
        if not messagebox.askyesno(
                'Limpar tabela',
                f'Remover todos os {len(self._pontos)} pontos da tabela de calibracao de LT?\n'
                'Esta acao nao pode ser desfeita.'):
            return
        self.tabela.delete(*self.tabela.get_children())
        self._pontos.clear()
        self._invalida_ajuste()

    def _invalida_ajuste(self):
        """Chamado sempre que a tabela de pontos muda: os coeficientes (e o
        grafico exportavel) de um ajuste anterior nao valem mais para os
        pontos atuais."""
        self._coefs_por_grau = None
        self.bt_exportar_grafico.configure(state='disabled')

    def _carrega_csv(self):
        caminho = filedialog.askopenfilename(
            title='Carregar calibracao_lt.csv',
            initialfile='calibracao_lt.csv', filetypes=[('CSV', '*.csv')])
        if not caminho:
            return
        try:
            with open(caminho, newline='') as arquivo:
                leitor = csv.DictReader(arquivo)
                if leitor.fieldnames is None or 'h_mm' not in leitor.fieldnames \
                        or 'contas' not in leitor.fieldnames:
                    messagebox.showerror(
                        'Arquivo invalido',
                        f'{caminho} nao tem as colunas h_mm e contas. '
                        'E um CSV de calibracao de LT (Tab. 1.6), salvo por '
                        '"salvar CSV (calibracao_lt.csv)" ou pelo hub_planta.py?')
                    return
                linhas = [(float(linha['h_mm']), int(float(linha['contas']))) for linha in leitor]
        except (OSError, ValueError) as erro:
            messagebox.showerror('Erro ao ler arquivo', f'Nao foi possivel ler {caminho}:\n{erro}')
            return
        if not linhas:
            messagebox.showwarning('Arquivo vazio', f'{caminho} nao tem nenhum ponto.')
            return

        # Cada linha entra exatamente como um clique em "adicionar ponto":
        # acrescida a self._pontos e inserida na tabela, sem substituir os
        # pontos ja presentes.
        for h, contas in linhas:
            self._pontos.append((h, contas))
            self.tabela.insert('', 'end', values=(f'{h:.1f}', contas))
        self._invalida_ajuste()
        messagebox.showinfo(
            'Carregado', f'{len(linhas)} pontos carregados de {caminho} e '
            'acrescentados a tabela.')

    def _salva_csv(self):
        if not self._pontos:
            messagebox.showwarning('Sem pontos', 'Adicione pelo menos um ponto antes de salvar.')
            return
        caminho = filedialog.asksaveasfilename(
            defaultextension='.csv', initialfile='calibracao_lt.csv',
            filetypes=[('CSV', '*.csv')])
        if not caminho:
            return
        with open(caminho, 'w', newline='') as arquivo:
            escritor = csv.writer(arquivo)
            escritor.writerow(['h_mm', 'contas'])
            for h, contas in self._pontos:
                escritor.writerow([f'{h:.2f}', contas])
        messagebox.showinfo('Salvo', f'{len(self._pontos)} pontos salvos em {caminho}.\n\n'
                            'Esse CSV tambem pode ser reprocessado depois por '
                            'ferramentas/calibracao-lt/calibra_lt.py.')

    def _ajusta(self):
        if len(self._pontos) < 4:
            messagebox.showwarning(
                'Poucos pontos', 'Sao necessarios pelo menos 4 pontos para um ajuste '
                'cubico nao ficar sobreajustado (a Tab. 1.6 tem 20).')
            return
        import numpy as np

        h = np.array([p[0] for p in self._pontos])
        contas = np.array([p[1] for p in self._pontos])
        ss_tot = float(np.sum((h - h.mean()) ** 2))

        # "R2" e nao "R²": o glifo unicode do sobrescrito nao tem a mesma
        # largura de um digito normal em varias fontes monoespacadas
        # (TkFixedFont incluida), o que desalinha a coluna com o cabecalho.
        linhas = [f'{"grau":>4} {"RMSE [mm]":>10} {"erro max [mm]":>14} {"R2":>8}']
        coefs_por_grau = {}
        coefs_grau3 = None
        for grau in (1, 2, 3):
            coefs = np.polyfit(contas, h, grau)
            h_pred = np.polyval(coefs, contas)
            residuos = h_pred - h
            rmse = float(np.sqrt(np.mean(residuos ** 2)))
            erro_max = float(np.max(np.abs(residuos)))
            r2 = 1.0 - float(np.sum(residuos ** 2)) / ss_tot
            linhas.append(f'{grau:>4} {rmse:>10.3f} {erro_max:>14.3f} {r2:>8.5f}')
            coefs_por_grau[grau] = coefs
            if grau == 3:
                coefs_grau3 = coefs

        linhas.append('')
        linhas.append('Equacoes ajustadas (h em mm, N = leitura de LT em contas). O vetor a direita')
        linhas.append('tem os mesmos coeficientes, do maior grau para o menor - selecione um numero')
        linhas.append('por vez e cole no campo correspondente de "Ajustar calibracao de LT":')
        for grau in (1, 2, 3):
            coefs = coefs_por_grau[grau]
            vetor = '[' + ', '.join(repr(float(c)) for c in coefs) + ']'
            linhas.append(f'  grau {grau}: {_formata_polinomio(coefs)}    coefs: {vetor}')

        linhas.append('')
        linhas.append('Cole em comum/conversoes.py, em LT_COEFS_H_DE_CONTAS:')
        linhas.append('LT_COEFS_H_DE_CONTAS = (' +
                      ', '.join(repr(float(c)) for c in coefs_grau3) + ')')

        self.txt_resultado.delete('1.0', 'end')
        self.txt_resultado.insert('1.0', '\n'.join(linhas))

        self._coefs_por_grau = coefs_por_grau
        self.bt_exportar_grafico.configure(state='normal')

    def _exporta_grafico(self):
        if not self._pontos or not self._coefs_por_grau:
            messagebox.showwarning(
                'Sem ajuste', 'Clique em "ajustar (graus 1-3) e mostrar coeficientes" '
                'antes de exportar o grafico.')
            return
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            import numpy as np
        except ImportError as erro:
            messagebox.showerror(
                'matplotlib nao encontrado',
                f'Nao foi possivel importar matplotlib/numpy para gerar o grafico:\n{erro}')
            return

        caminho = filedialog.asksaveasfilename(
            title='Exportar grafico da calibracao de LT',
            defaultextension='.pdf', initialfile='calibracao_lt.pdf',
            filetypes=[('PDF', '*.pdf')])
        if not caminho:
            return
        # Alguns Tk/macOS nao aplicam `defaultextension` de forma confiavel
        # quando ha so um filtro na caixa de dialogo - forcamos aqui para
        # nao acabar com um arquivo sem extensao (que o matplotlib tambem
        # recusaria a reconhecer como PDF).
        if not caminho.lower().endswith('.pdf'):
            caminho += '.pdf'

        h = np.array([p[0] for p in self._pontos])
        contas = np.array([p[1] for p in self._pontos])

        fig, eixo = plt.subplots(figsize=(7, 5))
        eixo.scatter(contas, h, color='#0a6ebd', s=28, zorder=3,
                     label='pontos medidos (Tab. 1.6)')

        contas_linha = np.linspace(contas.min(), contas.max(), 200)
        cores_grau = {1: '#d62728', 2: '#2ca02c', 3: '#7b2d8e'}
        for grau in (1, 2, 3):
            coefs = self._coefs_por_grau[grau]
            eixo.plot(contas_linha, np.polyval(coefs, contas_linha),
                      color=cores_grau[grau], linewidth=1.6, label=f'ajuste grau {grau}')

        eixo.set_xlabel('contas de LT_ADC')
        eixo.set_ylabel('h [mm]')
        eixo.set_title('Calibracao de LT (Secao 1.3.4 - Tab. 1.6)')
        eixo.grid(True, color='#e8e8e8')
        eixo.legend(loc='best', fontsize=9, frameon=False)

        try:
            fig.savefig(caminho, bbox_inches='tight')
        except Exception as erro:
            # Excecao ampla (nao so OSError) e proposital: um erro do
            # proprio matplotlib (ex.: backend/extensao) tambem precisa
            # aparecer aqui, em vez de so no console - foi assim que a
            # primeira versao deste botao falhava em silencio (o arquivo
            # nunca era escrito e nada avisava o usuario).
            messagebox.showerror('Erro ao salvar', f'Nao foi possivel salvar {caminho}:\n{erro}')
            return
        finally:
            plt.close(fig)
        messagebox.showinfo('Exportado', f'Grafico salvo em {os.path.abspath(caminho)}.')


class GravadorEnsaio:
    """Grava amostras num CSV consumivel por `ferramentas/ajuste-torricelli/
    ajusta_torricelli.py` (colunas t_s, lt_contas, h_mm, ft2_contas, qin_lpm,
    pump2_pct).

    Reamostra o fluxo continuo da thread de aquisicao (PERIODO_S) no periodo
    `periodo_s` pedido pelo usuario, so escrevendo uma linha quando ja se
    passou esse tempo desde a ultima linha gravada.

    A coluna `h_mm` sai de `h_fn`, e nao direto de `contas_para_altura`: quem
    constroi o gravador passa aqui o `contas_para_altura_ativa` da janela, de
    modo que o ensaio seja gravado com a MESMA calibracao de LT que o aluno
    ve na tabela de leituras e na curva de altura do grafico. Sem isso, colar
    a calibracao da propria bancada mudaria a tela e nao o arquivo.
    """

    def __init__(self, caminho, periodo_s, pump2_pct_fn, h_fn=contas_para_altura):
        self.caminho = caminho
        self.periodo_s = periodo_s
        self._pump2_pct_fn = pump2_pct_fn   # -> valor a gravar na coluna pump2_pct
        self._h_fn = h_fn                   # contas de LT -> h [mm]
        self._arquivo = open(caminho, 'w', newline='')
        self._escritor = csv.writer(self._arquivo)
        self._escritor.writerow(COLUNAS_ENSAIO)
        self.t0 = None
        self._ultimo_trel = None
        self.linhas = 0

    def recebe(self, t, valores):
        if self.t0 is None:
            self.t0 = t
        trel = t - self.t0
        if self._ultimo_trel is not None and trel - self._ultimo_trel < self.periodo_s - 1e-6:
            return trel
        self._ultimo_trel = trel

        lt = valores['LT']
        ft2 = valores['FT2']
        h = self._h_fn(lt)
        q_in = contas_para_vazao(ft2)
        pump2_pct = self._pump2_pct_fn()
        self._escritor.writerow([
            f'{trel:.3f}', lt, f'{h:.3f}', ft2, f'{q_in:.4f}',
            '' if pump2_pct is None else f'{pump2_pct:.1f}',
        ])
        self._arquivo.flush()
        self.linhas += 1
        return trel

    def fecha(self):
        self._arquivo.close()


class AbaAula2(AbaBase):
    """Aula 2 - ensaios de esvaziamento e de degrau (Secoes 2.3.2 e 2.3.3)."""

    def __init__(self, master, app):
        super().__init__(master, app)
        self._gravador_deg = None
        self._deg_estado = None   # dict com o estado do ensaio de degrau em curso
        self._mod_t = None        # arrays do CSV de degrau carregado para a validacao
        self._mod_h = None        # do modelo de 1a ordem (secao "Validacao do modelo")
        self._mod_qin = None
        self._mod_csv = None
        self._monta()

    def _monta(self):
        esv = ttk.LabelFrame(self, text='Ensaio de esvaziamento (Secao 2.3.2)', padding=10)
        esv.pack(fill='x', pady=(0, 10))
        ttk.Label(
            esv, text='O ensaio e feito direto no grafico ao vivo (nao ha gravacao\n'
                      'controlada por aqui): encha o tanque com o dreno fechado, abra o\n'
                      'dreno em t = 0 e, com o tanque parado perto de zero, use "exportar\n'
                      'dados" no grafico, arrastando do joelho da curva h ate o final da\n'
                      'descida (ver roteiro, Secao 2.3.2). Salve como esvaziamento-tanque.csv.',
            justify='left').grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 8))

        self.bt_esv = ttk.Button(
            esv, text='Exportar Gráficos do Ensaio de Esvaziamento',
            command=self._gera_grafico_esvaziamento)
        self.bt_esv.grid(row=1, column=0, sticky='w')
        self.lb_esv = ttk.Label(
            esv, text='le o esvaziamento-tanque.csv exportado e gera DOIS PDFs: um com\n'
                      'h(t) contra tres modelos de dreno (reta, Torricelli e Torricelli\n'
                      'com offset) e o residuo de cada um - a curvatura aparece no residuo,\n'
                      'nao em h(t) - e outro com sqrt(h)(t) e a reta ajustada, cujo\n'
                      'coeficiente angular alimenta a Analise (Secao 2.4, item 2).',
            justify='left')
        self.lb_esv.grid(row=1, column=1, sticky='w', padx=(10, 0))

        deg = ttk.LabelFrame(self, text='Ensaio de degrau (Secao 2.3.3)', padding=10)
        deg.pack(fill='x')
        ttk.Label(
            deg, text='Toma o controle de VALVE e PUMP2 durante o ensaio (os sliders ficam\n'
                      'bloqueados). Ao terminar, devolve o controle aos sliders, no ultimo\n'
                      'comando aplicado - sem zerar as saidas. Grava um CSV no mesmo formato\n'
                      'do exportado no ensaio de esvaziamento, com h_mm pela calibracao de\n'
                      'LT ativa.',
            justify='left').grid(row=0, column=0, columnspan=4, sticky='w', pady=(0, 8))

        campos = (
            ('valve (%)', 'var_deg_valve', '100'),
            ('PUMP2 inicial (%)', 'var_deg_pi', '45'),
            ('PUMP2 final (%)', 'var_deg_pf', '65'),
            ('t do degrau (s)', 'var_deg_tdeg', '20'),
            ('periodo T (s)', 'var_deg_T', '2'),
            # Ensaios reais (dreno totalmente aberto) medem tau da ordem de
            # 100-150 s na faixa usual de operacao - acomodamento completo
            # (~4 tau) pode levar 7 a 10 min. Deixa em branco por padrao
            # ("ate parar") em vez de arriscar um corte antes do regime
            # permanente; o aluno decide quando encerrar, olhando o grafico.
            ('duracao (s, vazio = ate parar)', 'var_deg_dur', ''),
        )
        for i, (rotulo, nome, padrao) in enumerate(campos):
            var = tk.StringVar(value=padrao)
            setattr(self, nome, var)
            ttk.Label(deg, text=rotulo + ':').grid(row=1 + i // 3, column=2 * (i % 3), sticky='w')
            ttk.Entry(deg, textvariable=var, width=8).grid(
                row=1 + i // 3, column=2 * (i % 3) + 1, sticky='w', padx=(4, 20))

        ttk.Label(deg, text='arquivo:').grid(row=3, column=0, sticky='w')
        self.var_deg_arquivo = tk.StringVar(value='degrau.csv')
        ttk.Entry(deg, textvariable=self.var_deg_arquivo, width=24).grid(
            row=3, column=1, columnspan=3, sticky='w', padx=(4, 0))

        self.bt_deg = ttk.Button(deg, text='iniciar ensaio', command=self._alterna_deg)
        self.bt_deg.grid(row=4, column=0, columnspan=2, sticky='w', pady=(8, 0))
        self.lb_deg = ttk.Label(deg, text='parado.')
        self.lb_deg.grid(row=4, column=2, columnspan=4, sticky='w', pady=(8, 0))

        mod = ttk.LabelFrame(self, text='Validacao do modelo de 1a ordem (Analise, Secao 2.4, itens 4-5)', padding=10)
        mod.pack(fill='x', pady=(10, 0))
        ttk.Label(
            mod, text='Carregue o degrau.csv do ensaio acima e informe os valores que voce\n'
                      'ja calculou na Analise (h0, q_in0, q_in1 da Tab. de ponto de operacao,\n'
                      'e K, tau do modelo linearizado) para gerar um PDF sobrepondo a curva\n'
                      'medida a curva simulada do modelo. O instante do degrau e localizado\n'
                      'automaticamente pelo salto em FT2 (qin_lpm) nos dados carregados.',
            justify='left').grid(row=0, column=0, columnspan=4, sticky='w', pady=(0, 8))

        campos_mod = (
            ('h0 (mm)', 'var_mod_h0'),
            ('q_in0 (L/min)', 'var_mod_qin0'),
            ('q_in1 (L/min)', 'var_mod_qin1'),
            ('K (mm/(L/min))', 'var_mod_K'),
            ('tau (s)', 'var_mod_tau'),
        )
        self._vars_mod = []
        for i, (rotulo, nome) in enumerate(campos_mod):
            var = tk.StringVar(value='')
            setattr(self, nome, var)
            self._vars_mod.append(var)
            ttk.Label(mod, text=rotulo + ':').grid(row=1 + i // 3, column=2 * (i % 3), sticky='w')
            ttk.Entry(mod, textvariable=var, width=8).grid(
                row=1 + i // 3, column=2 * (i % 3) + 1, sticky='w', padx=(4, 20))

        self.bt_mod_carrega = ttk.Button(
            mod, text='Carregar CSV do Ensaio de Degrau', command=self._carrega_csv_degrau)
        self.bt_mod_carrega.grid(row=3, column=0, columnspan=2, sticky='w', pady=(8, 0))
        # So habilitado com um CSV carregado e os cinco campos preenchidos com
        # numeros validos - ver `_atualiza_estado_mod_grafico`, chamado a cada
        # edicao de campo (trace abaixo) e depois de carregar/descartar o CSV.
        self.bt_mod_grafico = ttk.Button(
            mod, text='Gerar Gráfico Comparativo (PDF)', command=self._gera_grafico_modelo_degrau,
            state='disabled')
        self.bt_mod_grafico.grid(row=3, column=2, columnspan=2, sticky='w', pady=(8, 0))
        self.lb_mod = ttk.Label(mod, text='nenhum CSV carregado.', justify='left')
        self.lb_mod.grid(row=4, column=0, columnspan=4, sticky='w', pady=(8, 0))

        # Registrado so agora, com bt_mod_grafico ja existente: o callback do
        # trace le/escreve esse botao, entao precisa dele pronto antes de
        # qualquer 'write' poder disparar.
        for var in self._vars_mod:
            var.trace_add('write', self._atualiza_estado_mod_grafico)

    # -- ensaio de esvaziamento ---------------------------------------------

    def _gera_grafico_esvaziamento(self):
        caminho_csv = filedialog.askopenfilename(
            title='Abrir CSV do ensaio de esvaziamento',
            initialfile='esvaziamento-tanque.csv',
            filetypes=[('CSV', '*.csv'), ('todos os arquivos', '*.*')])
        if not caminho_csv:
            return

        try:
            t, h = self._le_csv_esvaziamento(caminho_csv)
        except ValueError as erro:
            messagebox.showerror('CSV invalido', str(erro))
            return

        base = os.path.splitext(os.path.basename(caminho_csv))[0]

        # -- grafico 1: h(t) contra tres modelos de dreno, com residuo ------
        caminho_modelos = filedialog.asksaveasfilename(
            title='Salvar grafico de h(t) contra os tres modelos de dreno',
            defaultextension='.pdf', initialfile=f'{base}-modelos.pdf',
            filetypes=[('PDF', '*.pdf')])
        if not caminho_modelos:
            return

        erro = self._salva_pdf_modelos_esvaziamento(caminho_modelos, t, h)
        if erro:
            messagebox.showerror('Nao foi possivel gerar o grafico dos modelos', erro)
            return

        # -- grafico 2: sqrt(h) linearizado, com a reta ajustada -------------
        try:
            import numpy as np
        except ImportError as erro:
            messagebox.showerror('numpy ausente', str(erro))
            return

        # Descarta leituras negativas (ruido do LT perto de h=0) antes da raiz
        # - mesma guarda do `--h-min` (padrao 0) de `ajusta_torricelli.py`.
        valido = h >= 0.0
        if valido.sum() < 2:
            messagebox.showerror(
                'Sem amostras validas',
                'Depois de descartar leituras de h negativas (ruido perto de zero), '
                'sobraram menos de duas amostras para ajustar sqrt(h).')
            return
        t_raiz, h_raiz = t[valido], h[valido]

        raiz_h = np.sqrt(h_raiz)
        a, b = np.polyfit(t_raiz, raiz_h, 1)
        if a >= 0:
            messagebox.showwarning(
                'Coeficiente angular positivo',
                'sqrt(h) cresce ao longo do CSV, em vez de decair - confira se o '
                'arquivo e mesmo o do ensaio de esvaziamento (Secao 2.3.2) e se o '
                'recorte nao pegou o trecho de enchimento por engano. O grafico '
                'sera gerado assim mesmo.')

        caminho_raiz = filedialog.asksaveasfilename(
            title='Salvar grafico de sqrt(h) (linearizacao de Torricelli)',
            defaultextension='.pdf', initialfile=f'{base}-raiz.pdf',
            filetypes=[('PDF', '*.pdf')])
        if not caminho_raiz:
            self.lb_esv.configure(
                text=f'grafico dos modelos salvo em {caminho_modelos}.\n'
                     'grafico de sqrt(h) cancelado.',
                justify='left')
            return

        erro = self._salva_pdf_raiz_esvaziamento(caminho_raiz, t_raiz, h_raiz, a, b)
        if erro:
            messagebox.showerror('Nao foi possivel gerar o grafico de sqrt(h)', erro)
            return

        self.lb_esv.configure(
            text=f'modelos: {caminho_modelos}\n'
                 f'sqrt(h): {caminho_raiz}\n'
                 f'coeficiente angular da reta ajustada: a = {a:.5f} mm^0.5/s '
                 f'(use-o na Analise, Secao 2.4, item 2).',
            justify='left')

    def _le_csv_esvaziamento(self, caminho):
        """Le as colunas t_s e h_mm de um CSV exportado do grafico ao vivo ou
        gravado por `GravadorEnsaio` - ambos usam essas mesmas colunas (ver
        `ferramentas/ajuste-torricelli/ajusta_torricelli.py`)."""
        import numpy as np
        t, h = [], []
        with open(caminho, newline='') as arquivo:
            leitor = csv.DictReader(arquivo)
            if leitor.fieldnames is None or 't_s' not in leitor.fieldnames \
                    or 'h_mm' not in leitor.fieldnames:
                raise ValueError(
                    f'{caminho} nao tem as colunas t_s e h_mm. Ele foi exportado '
                    'do grafico ao vivo (botao "exportar dados") ou gerado por '
                    'esse mesmo hub?')
            for linha in leitor:
                t.append(float(linha['t_s']))
                h.append(float(linha['h_mm']))
        if len(t) < 2:
            raise ValueError(f'{caminho} tem menos de duas amostras.')
        return np.array(t), np.array(h)

    def _importa_modelos_esvaziamento(self):
        """Reaproveita os modelos de dreno (reta, Torricelli puro, Torricelli
        com offset) e o ajuste por `scipy.optimize.curve_fit` ja escritos em
        `ferramentas/analise-rampas/analisa_rampas.py` (usado para gerar as
        figuras de modelagem empirica da Aula 3) - so as funcoes de ajuste,
        nao a estetica das figuras la definidas."""
        caminho_ferramenta = os.path.join(RAIZ, 'ferramentas', 'analise-rampas')
        if caminho_ferramenta not in sys.path:
            sys.path.insert(0, caminho_ferramenta)
        import analisa_rampas
        return analisa_rampas

    def _salva_pdf_modelos_esvaziamento(self, caminho, t, h):
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            import numpy as np
        except ImportError as erro:
            return str(erro)

        try:
            modelos = self._importa_modelos_esvaziamento()
        except ImportError as erro:
            return f'{erro} (este grafico tambem exige scipy, alem de matplotlib/numpy)'

        try:
            reta, p_tor, p_off = modelos.ajusta_modelos(t, h)
        except RuntimeError as erro:
            return (f'o ajuste de Torricelli (scipy.optimize.curve_fit) nao '
                    f'convergiu neste CSV: {erro}')

        curvas = [
            ('reta', np.polyval(reta, t)),
            (r'Torricelli $\sqrt{h}$', modelos.h_torricelli(t, *p_tor)),
            (r'Torricelli $\sqrt{h+H_0}$', modelos.h_torricelli_offset(t, *p_off)),
        ]

        fig, (ax_h, ax_res) = plt.subplots(
            2, 1, figsize=(7, 7), sharex=True, gridspec_kw={'height_ratios': [2, 1.3]})

        ax_h.plot(t, h, 'o', markersize=4, color='0.35', label='medido')
        for nome, y in curvas:
            ax_h.plot(t, y, '-', linewidth=1.5, label=nome)
        ax_h.set_ylabel('$h$  [mm]')
        ax_h.set_title('Ensaio de esvaziamento: h(t) contra tres modelos de dreno')
        # Tambem fora do eixo, a direita, e no mesmo x da legenda de baixo -
        # com sharex, uma legenda so no painel de baixo deixaria os dois
        # eixos com larguras diferentes (o de cima ficaria mais largo).
        ax_h.legend(fontsize=8, loc='upper left', bbox_to_anchor=(1.02, 1.0),
                   borderaxespad=0.0)
        ax_h.grid(True, alpha=0.3)

        for nome, y in curvas:
            r = h - y
            rms = float(np.sqrt(np.mean(r ** 2)))
            maximo = float(np.abs(r).max())
            ax_res.plot(t, r, '-', linewidth=1.5,
                       label=f'{nome} (RMS = {rms:.2f} mm, max = {maximo:.2f} mm)')
        ax_res.axhline(0.0, color='0.5', linewidth=0.8)
        ax_res.set_xlabel('$t$  [s]')
        ax_res.set_ylabel(r'residuo  $h - \mathrm{modelo}$  [mm]')
        # Legenda fora do eixo (a direita): dentro do eixo ela sempre acaba
        # em cima de alguma curva ou do texto abaixo, dependendo dos dados
        # de cada ensaio - 'loc=best' nao tem um canto livre garantido aqui.
        ax_res.legend(fontsize=8, loc='upper left', bbox_to_anchor=(1.02, 1.0),
                      borderaxespad=0.0)
        ax_res.grid(True, alpha=0.3)
        # Texto no canto inferior esquerdo, DENTRO do eixo: com a legenda
        # movida para fora, esse canto fica livre.
        ax_res.text(
            0.02, 0.03,
            'repare o arco no residuo da reta: e onde a curvatura de Torricelli\n'
            'aparece - os residuos dos outros dois modelos ficam mais proximos\n'
            'de zero e sem esse padrao sistematico.',
            transform=ax_res.transAxes, fontsize=7, va='bottom', ha='left')

        fig.tight_layout()
        fig.savefig(caminho, dpi=150, bbox_inches='tight')
        plt.close(fig)
        return None

    def _salva_pdf_raiz_esvaziamento(self, caminho, t, h, a, b):
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            import numpy as np
        except ImportError as erro:
            return str(erro)

        fig, ax = plt.subplots(figsize=(7, 4.5))
        ax.plot(t, np.sqrt(h), 'o', markersize=4, label='medido')
        ax.plot(t, a * t + b, '-',
               label=f'reta ajustada: $\\sqrt{{h}} = {b:.3f} {a:+.4f}\\,t$')
        ax.set_xlabel('$t$  [s]')
        ax.set_ylabel(r'$\sqrt{h}$  [mm$^{1/2}$]')
        ax.set_title('Ensaio de esvaziamento: linearizacao de Torricelli')
        ax.legend()
        ax.grid(True, alpha=0.3)

        fig.tight_layout()
        fig.savefig(caminho, dpi=150)
        plt.close(fig)
        return None

    # -- ensaio de degrau -----------------------------------------------

    def _alterna_deg(self):
        if self._deg_estado is None:
            self._inicia_deg()
        else:
            self._encerra_deg('interrompido pelo usuario')

    def _le_float(self, var, nome, minimo=None, maximo=None):
        try:
            valor = float(var.get().replace(',', '.'))
        except ValueError:
            raise ValueError(f'{nome} precisa ser um numero.')
        if minimo is not None and valor < minimo:
            raise ValueError(f'{nome} tem de ser >= {minimo}.')
        if maximo is not None and valor > maximo:
            raise ValueError(f'{nome} tem de ser <= {maximo}.')
        return valor

    def _inicia_deg(self):
        try:
            valve = self._le_float(self.var_deg_valve, 'valve (%)', 0, 100)
            p_ini = self._le_float(self.var_deg_pi, 'PUMP2 inicial (%)', 0, 100)
            p_fim = self._le_float(self.var_deg_pf, 'PUMP2 final (%)', 0, 100)
            t_deg = self._le_float(self.var_deg_tdeg, 't do degrau (s)', 0)
            T = self._le_float(self.var_deg_T, 'periodo T (s)', 1e-6)
            dur_txt = self.var_deg_dur.get().strip()
            dur = self._le_float(self.var_deg_dur, 'duracao (s)', 0) if dur_txt else None
        except ValueError as erro:
            messagebox.showerror('Parametro invalido', str(erro))
            return
        if dur is not None and dur <= t_deg:
            messagebox.showerror(
                'Parametro invalido',
                'A duracao tem de ser maior que o t do degrau, senao o degrau '
                'nunca chega a ser aplicado.')
            return
        if valve < VALVE_MIN_COM_BOMBA - 1e-6:
            messagebox.showerror(
                'Intertravamento',
                f'valve (%) = {valve:.0f} bloquearia PUMP2 (PUMP2 so fica ligada com '
                f'VALVE em pelo menos {VALVE_MIN_COM_BOMBA:.0f} %).')
            return

        if not self.app.confirma_calibracao_lt('ensaio de degrau'):
            return
        if not self.app.pede_controle('Aula 2 - ensaio de degrau'):
            return

        caminho = self.var_deg_arquivo.get().strip() or 'degrau.csv'
        self._deg_estado = {
            'valve': valve, 'p_ini': p_ini, 'p_fim': p_fim, 't_deg': t_deg,
            'dur': dur, 'aplicado': False, 'pump2_atual': p_ini,
        }
        self._gravador_deg = GravadorEnsaio(
            caminho, T, pump2_pct_fn=lambda: self._deg_estado['pump2_atual'],
            h_fn=self.app.contas_para_altura_ativa)
        self.app.aplica_comando(valve, p_ini)
        self.bt_deg.configure(text='parar ensaio')
        self.lb_deg.configure(
            text=f'condicao inicial aplicada (VALVE {valve:.0f} %, PUMP2 {p_ini:.0f} %); '
                 f'degrau em t = {t_deg:.0f} s.')

    def _encerra_deg(self, motivo):
        n = self._gravador_deg.linhas if self._gravador_deg else 0
        caminho = self._gravador_deg.caminho if self._gravador_deg else ''
        if self._gravador_deg is not None:
            self._gravador_deg.fecha()
        self._gravador_deg = None
        self._deg_estado = None
        self.app.libera_controle()
        self.bt_deg.configure(text='iniciar ensaio')
        self.lb_deg.configure(text=f'parado ({motivo}). {n} amostras salvas em {caminho}.')

    def atualiza_amostra(self, t, valores):
        if self._deg_estado is not None and self._gravador_deg is not None:
            estado = self._deg_estado
            trel = self._gravador_deg.recebe(t, valores)

            if not estado['aplicado'] and trel >= estado['t_deg']:
                estado['aplicado'] = True
                estado['pump2_atual'] = estado['p_fim']
                self.app.aplica_comando(estado['valve'], estado['p_fim'])
                self.lb_deg.configure(
                    text=f'degrau aplicado em t = {trel:.1f} s: '
                         f'PUMP2 -> {estado["p_fim"]:.0f} %.')

            if estado['dur'] is not None and trel >= estado['dur']:
                self._encerra_deg('duracao atingida')

    # -- validacao do modelo de 1a ordem -------------------------------------

    def _le_csv_degrau(self, caminho):
        """Le as colunas t_s, h_mm e qin_lpm de um degrau.csv exportado do
        grafico ao vivo ou gravado por `GravadorEnsaio` - ambos os schemas
        (COLUNAS_EXPORTACAO e COLUNAS_ENSAIO) tem essas tres colunas."""
        import numpy as np
        t, h, qin = [], [], []
        with open(caminho, newline='') as arquivo:
            leitor = csv.DictReader(arquivo)
            faltando = [c for c in ('t_s', 'h_mm', 'qin_lpm')
                        if leitor.fieldnames is None or c not in leitor.fieldnames]
            if faltando:
                raise ValueError(
                    f'{caminho} nao tem a(s) coluna(s) {", ".join(faltando)}. Ele foi '
                    'exportado do grafico ao vivo (botao "exportar dados") ou gravado '
                    'pelo ensaio de degrau deste hub?')
            for linha in leitor:
                t.append(float(linha['t_s']))
                h.append(float(linha['h_mm']))
                qin.append(float(linha['qin_lpm']))
        if len(t) < 2:
            raise ValueError(f'{caminho} tem menos de duas amostras.')
        return np.array(t), np.array(h), np.array(qin)

    def _carrega_csv_degrau(self):
        caminho_csv = filedialog.askopenfilename(
            title='Abrir CSV do ensaio de degrau',
            initialfile='degrau.csv',
            filetypes=[('CSV', '*.csv'), ('todos os arquivos', '*.*')])
        if not caminho_csv:
            return
        try:
            t, h, qin = self._le_csv_degrau(caminho_csv)
        except (OSError, ValueError) as erro:
            messagebox.showerror('CSV invalido', str(erro))
            return
        self._mod_t, self._mod_h, self._mod_qin = t, h, qin
        self._mod_csv = caminho_csv
        self.lb_mod.configure(
            text=f'{os.path.basename(caminho_csv)} carregado: {len(t)} amostras.')
        self._atualiza_estado_mod_grafico()

    def _atualiza_estado_mod_grafico(self, *_args):
        """Habilita 'Gerar Grafico Comparativo' so quando ha um CSV carregado
        E os cinco campos numericos tem valores validos - chamado pelo trace
        de cada var_mod_* e apos carregar (ou falhar ao carregar) um CSV."""
        habilitado = self._mod_t is not None and all(
            self._campo_mod_valido(var) for var in self._vars_mod)
        self.bt_mod_grafico.configure(state='normal' if habilitado else 'disabled')

    @staticmethod
    def _campo_mod_valido(var):
        texto = var.get().strip()
        if not texto:
            return False
        try:
            float(texto.replace(',', '.'))
        except ValueError:
            return False
        return True

    def _gera_grafico_modelo_degrau(self):
        if self._mod_t is None:
            messagebox.showerror(
                'Nenhum CSV carregado',
                'Carregue primeiro o CSV do ensaio de degrau (botao acima).')
            return

        try:
            h0 = self._le_float(self.var_mod_h0, 'h0 (mm)')
            qin0 = self._le_float(self.var_mod_qin0, 'q_in0 (L/min)')
            qin1 = self._le_float(self.var_mod_qin1, 'q_in1 (L/min)')
            K = self._le_float(self.var_mod_K, 'K (mm/(L/min))')
            tau = self._le_float(self.var_mod_tau, 'tau (s)', 1e-6)
        except ValueError as erro:
            messagebox.showerror('Parametro invalido', str(erro))
            return
        if abs(qin1 - qin0) < 1e-9:
            messagebox.showerror(
                'Parametro invalido',
                'q_in0 e q_in1 estao iguais - nao ha degrau para sincronizar nem '
                'simular.')
            return

        t, h, qin = self._mod_t, self._mod_h, self._mod_qin
        meio = (qin0 + qin1) / 2.0
        import numpy as np
        cruza = (qin >= meio) if qin1 > qin0 else (qin <= meio)
        if not cruza.any():
            messagebox.showerror(
                'Degrau nao encontrado',
                'Nao foi possivel localizar o salto de FT2 (qin_lpm) nos dados '
                'carregados, usando o limiar (q_in0 + q_in1) / 2. Confira se o CSV '
                'cobre o instante do degrau e se q_in0/q_in1 correspondem aos '
                'patamares reais do ensaio.')
            return
        i_deg = int(np.argmax(cruza))
        t_deg = t[i_deg]

        h_sim = np.where(
            t < t_deg, h0,
            h0 + K * (qin1 - qin0) * (1.0 - np.exp(-(t - t_deg) / tau)))

        base = os.path.splitext(os.path.basename(self._mod_csv))[0]
        caminho_pdf = filedialog.asksaveasfilename(
            title='Salvar grafico comparativo (medido x modelo de 1a ordem)',
            defaultextension='.pdf', initialfile=f'{base}-modelo.pdf',
            filetypes=[('PDF', '*.pdf')])
        if not caminho_pdf:
            return

        erro = self._salva_pdf_modelo_degrau(caminho_pdf, t, h, h_sim, t_deg)
        if erro:
            messagebox.showerror('Nao foi possivel gerar o grafico', erro)
            return

        pos_deg = t >= t_deg
        rms = float(np.sqrt(np.mean((h[pos_deg] - h_sim[pos_deg]) ** 2)))
        self.lb_mod.configure(
            text=f'grafico salvo em {caminho_pdf}\n'
                 f'degrau localizado em t = {t_deg:.1f} s; '
                 f'RMS medido-modelo (t >= t do degrau) = {rms:.2f} mm.',
            justify='left')

    def _salva_pdf_modelo_degrau(self, caminho, t, h, h_sim, t_deg):
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
        except ImportError as erro:
            return str(erro)

        fig, ax = plt.subplots(figsize=(7, 4.5))
        ax.plot(t, h, 'o', markersize=3, color='0.35', label='medido')
        ax.plot(t, h_sim, '-', linewidth=1.5, label='modelo de 1a ordem')
        ax.axvline(t_deg, color='0.5', linestyle=':', linewidth=1.0,
                   label=f'degrau detectado (t = {t_deg:.1f} s)')
        ax.set_xlabel('$t$  [s]')
        ax.set_ylabel('$h$  [mm]')
        ax.set_title('Ensaio de degrau: medido contra modelo linearizado de 1a ordem')
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)

        fig.tight_layout()
        fig.savefig(caminho, dpi=150)
        plt.close(fig)
        return None


class AbaAula3(AbaBase):
    """Aula 3 - varredura estatica do atuador e escada de degraus (Secoes 3.3.1 e 3.3.2)."""

    # Janela sobre a qual a escada mede a DERIVA do nivel no fim de cada
    # patamar. O roteiro da Aula 3 pede um criterio quantitativo (e nao
    # visual) de acomodacao - "h deve variar menos que uns poucos milimetros
    # ao longo dos ultimos 30 s do patamar" -, e e este numero que o hub
    # coloca na tabela ao vivo e no CSV de equilibrios. Sem ele o aluno so
    # tem o olho sobre o grafico, que e justamente o que o roteiro proibe.
    DERIVA_JANELA_S = 30.0

    def __init__(self, master, app):
        super().__init__(master, app)
        self._var_estado = None
        self._var_arquivo = None
        self._var_escritor = None
        # Patamares da ultima varredura, ja em numeros: (u, sentido, qin, h).
        # A tabela ao vivo guarda os mesmos valores, mas formatados como texto
        # e arredondados para exibicao - o grafico le daqui para nao depender
        # dessa formatacao nem do CSV, que ja foi fechado quando o aluno
        # clica em "salvar grafico".
        self._var_pontos = []
        self._esc_estado = None
        self._esc_gravador = None
        self._esc_arquivo_eq = None
        self._esc_escritor_eq = None
        self._monta()

    def _monta(self):
        var = ttk.LabelFrame(self, text='Varredura estatica do atuador (Secao 3.3.1)', padding=10)
        var.pack(fill='x', pady=(0, 10))
        ttk.Label(
            var, text='Percorre uma lista de comandos de PUMP2, subindo ate o comando final e\n'
                      'depois descendo de volta ao inicial, mantendo cada patamar pelo tempo de\n'
                      'permanencia e gravando a media de FT2 dos ultimos segundos de cada um.\n'
                      'Registra tambem o h medio (em mm, pela calibracao de LT ativa) de cada\n'
                      'patamar - o tanque enche durante a varredura, e e essa coluna que mostra\n'
                      'que qin(u) nao depende do nivel. ATENCAO: essa coluna h NAO e a curva\n'
                      'estatica do processo - com 20 s por patamar e tau > 100 s, o nivel esta\n'
                      'sempre em transitorio, e subida e descida fecham um laco largo que NAO e\n'
                      'histerese. heq(u) vem da escada abaixo, de 600 s por patamar.\n'
                      'O botao "salvar grafico qin x u" gera a figura da curva do atuador,\n'
                      'com subida e descida como series separadas (item 1 da analise).\n'
                      'Exige VALVE em pelo menos 30 % (slider/botao do topo da janela).',
            justify='left').grid(row=0, column=0, columnspan=6, sticky='w', pady=(0, 8))

        campos_var = (
            ('comando inicial (%)', 'var_var_ini', '0'),
            ('comando final (%)', 'var_var_fim', '100'),
            ('passo (%)', 'var_var_passo', '5'),
            # 20 s: o que o roteiro da Aula 3 pede para a varredura de 0 a 100 %.
            # FT2 acomoda em ~3 s, entao sobra folga - MENOS para o patamar em
            # que a bomba parte (~15 %), que nao acomoda nem em 20 s. Por isso o
            # roteiro pede uma segunda varredura, fina, de 10 a 25 % com 60 s.
            ('permanencia por patamar (s)', 'var_var_perm', '20'),
            ('media dos ultimos (s)', 'var_var_media', '5'),
        )
        for i, (rotulo, nome, padrao) in enumerate(campos_var):
            v = tk.StringVar(value=padrao)
            setattr(self, nome, v)
            ttk.Label(var, text=rotulo + ':').grid(row=1 + i // 3, column=2 * (i % 3), sticky='w')
            ttk.Entry(var, textvariable=v, width=8).grid(
                row=1 + i // 3, column=2 * (i % 3) + 1, sticky='w', padx=(4, 20))

        ttk.Label(var, text='arquivo:').grid(row=3, column=0, sticky='w')
        # Sugestao levada ao dialogo de "salvar como" aberto por
        # `_inicia_var`; depois da escolha, passa a mostrar o caminho
        # efetivamente usado, para que o aluno saiba onde o ensaio foi parar.
        self.var_var_arquivo = tk.StringVar(value=ARQUIVO_VARREDURA)
        ttk.Entry(var, textvariable=self.var_var_arquivo, width=24).grid(
            row=3, column=1, columnspan=3, sticky='w', padx=(4, 0))

        self.bt_var = ttk.Button(var, text='iniciar varredura', command=self._alterna_var)
        self.bt_var.grid(row=4, column=0, columnspan=2, sticky='w', pady=(8, 0))
        # O grafico so faz sentido depois que ha patamares medidos, e a
        # varredura dura minutos - por isso o botao nasce desabilitado e e
        # liberado pela primeira linha gravada em `_atualiza_var`.
        self.bt_var_graf = ttk.Button(var, text='salvar grafico qin x u',
                                      command=self._salva_grafico_var, state='disabled')
        self.bt_var_graf.grid(row=4, column=2, columnspan=2, sticky='w', pady=(8, 0))
        self.lb_var = ttk.Label(var, text='parado.')
        self.lb_var.grid(row=5, column=0, columnspan=6, sticky='w', pady=(4, 0))

        # A varredura padrao (0 a 100 % de 5 em 5) rende 41 patamares, e o
        # roteiro pede que TODAS as 41 linhas sejam conferidas. Com altura 5 e
        # sem barra de rolagem, as primeiras sumiam de vista sem nenhum indicio
        # de que ainda estavam la - dai o quadro com barra e a altura maior.
        quadro_var = ttk.Frame(var)
        quadro_var.grid(row=6, column=0, columnspan=6, sticky='nsew', pady=(8, 0))
        quadro_var.rowconfigure(0, weight=1)
        quadro_var.columnconfigure(0, weight=1)
        self.tabela_var = ttk.Treeview(
            quadro_var, columns=('u', 'sentido', 'qin', 'h'), show='headings', height=11)
        self.tabela_var.heading('u', text='u [%]')
        self.tabela_var.heading('sentido', text='sentido')
        self.tabela_var.heading('qin', text='qin medio [L/min]')
        self.tabela_var.heading('h', text='h medio [mm]')
        barra_var = ttk.Scrollbar(quadro_var, orient='vertical', command=self.tabela_var.yview)
        self.tabela_var.configure(yscrollcommand=barra_var.set)
        self.tabela_var.grid(row=0, column=0, sticky='nsew')
        barra_var.grid(row=0, column=1, sticky='ns')

        esc = ttk.LabelFrame(self, text='Escada de degraus (Secao 3.3.2)', padding=10)
        esc.pack(fill='both', expand=True)
        ttk.Label(
            esc, text='Aplica uma sequencia de comandos de PUMP2, cada um mantido pela mesma\n'
                      'duracao, gravando um CSV continuo (t_s, lt_contas, h_mm, ft2_contas,\n'
                      'qin_lpm, pump2_pct) e, num segundo arquivo, o instante de inicio, o h de\n'
                      'equilibrio e o qin de equilibrio de cada patamar (Tab. 3.1 e Tab. 3.2).\n'
                      'Cada patamar traz ainda a DERIVA de h nos seus ultimos 30 s: e o\n'
                      'criterio quantitativo de acomodacao do roteiro - poucos mm significam\n'
                      'patamar acomodado; a linha fica em vermelho se passar de 3 mm.\n'
                      'Nos dois arquivos, h sai em mm pela calibracao de LT ativa no painel de\n'
                      'leituras - a mesma da curva h do grafico.',
            justify='left').grid(row=0, column=0, columnspan=6, sticky='w', pady=(0, 8))

        ttk.Label(esc, text='sequencia de comandos (%):').grid(row=1, column=0, sticky='w')
        self.var_esc_seq = tk.StringVar(value='50,70,85,70,50')
        ttk.Entry(esc, textvariable=self.var_esc_seq, width=28).grid(
            row=1, column=1, columnspan=3, sticky='w', padx=(4, 0))

        campos_esc = (
            # 600 s (~4 tau no patamar mais alto; o degrau 50->70 % da Aula 2
            # teve tau ~130 s e chegou a 98 % em ~510 s) - ver roteiro da Aula 3.
            ('duracao por patamar (s)', 'var_esc_dur', '600'),
            ('periodo T (s)', 'var_esc_T', '1'),
            ('media dos ultimos (s)', 'var_esc_media', '10'),
        )
        for i, (rotulo, nome, padrao) in enumerate(campos_esc):
            v = tk.StringVar(value=padrao)
            setattr(self, nome, v)
            ttk.Label(esc, text=rotulo + ':').grid(row=2, column=2 * i, sticky='w', pady=(6, 0))
            ttk.Entry(esc, textvariable=v, width=8).grid(
                row=2, column=2 * i + 1, sticky='w', padx=(4, 20), pady=(6, 0))

        # Sem campo de arquivo: pasta e nome sao escolhidos num dialogo de
        # "salvar como" aberto pelo proprio "iniciar escada", como na
        # varredura acima. O caminho efetivamente usado aparece no rotulo de
        # status ao fim do ensaio.
        self.bt_esc = ttk.Button(esc, text='iniciar escada', command=self._alterna_esc)
        self.bt_esc.grid(row=3, column=0, columnspan=2, sticky='w', pady=(8, 0))
        self.lb_esc = ttk.Label(esc, text='parado.')
        self.lb_esc.grid(row=3, column=2, columnspan=4, sticky='w', pady=(8, 0))

        self.tabela_esc = ttk.Treeview(
            esc, columns=('patamar', 'u', 't0', 'heq', 'qineq', 'deriva'),
            show='headings', height=6)
        for coluna, texto in (('patamar', 'patamar'), ('u', 'u [%]'), ('t0', 't inicio [s]'),
                              ('heq', 'h_eq [mm]'), ('qineq', 'qin_eq [L/min]'),
                              ('deriva', f'deriva {self.DERIVA_JANELA_S:.0f} s [mm]')):
            self.tabela_esc.heading(coluna, text=texto)
        # Patamar que ainda nao acomodou sai em vermelho: e o unico aviso que o
        # aluno recebe a tempo de refazer a escada com patamares mais longos.
        self.tabela_esc.tag_configure('instavel', foreground='#a11')
        self.tabela_esc.grid(row=4, column=0, columnspan=6, sticky='nsew', pady=(8, 0))
        esc.rowconfigure(4, weight=1)
        esc.columnconfigure(5, weight=1)

    # -- helpers -------------------------------------------------------

    def _le_float(self, var, nome, minimo=None, maximo=None):
        try:
            valor = float(var.get().replace(',', '.'))
        except ValueError:
            raise ValueError(f'{nome} precisa ser um numero.')
        if minimo is not None and valor < minimo:
            raise ValueError(f'{nome} tem de ser >= {minimo}.')
        if maximo is not None and valor > maximo:
            raise ValueError(f'{nome} tem de ser <= {maximo}.')
        return valor

    def _janela_media(self, amostras, trel, media_s):
        """Media dos valores cujo instante cai nos ultimos `media_s` s antes de `trel`."""
        vistos = [v for tt, v in amostras if trel - tt <= media_s]
        return sum(vistos) / len(vistos) if vistos else float('nan')

    def _deriva_h(self, buffer, trel):
        """Quanto h ainda andou nos ultimos `DERIVA_JANELA_S` s do patamar.

        Positivo = ainda subindo; negativo = ainda descendo. E a traducao
        numerica do criterio de acomodacao do roteiro, que de outro modo o
        aluno so conseguiria julgar no olho, sobre a curva do grafico.
        """
        janela = [hh for tt, hh, _q in buffer if trel - tt <= self.DERIVA_JANELA_S]
        if len(janela) < 2:
            return float('nan')
        return janela[-1] - janela[0]

    @staticmethod
    def _mmss(segundos):
        segundos = max(0, int(round(segundos)))
        return f'{segundos // 60:d}:{segundos % 60:02d}'

    # -- varredura estatica ----------------------------------------------

    def _alterna_var(self):
        if self._var_estado is None:
            self._inicia_var()
        else:
            self._encerra_var('interrompida pelo usuario')

    def _inicia_var(self):
        try:
            ini = self._le_float(self.var_var_ini, 'comando inicial (%)', 0, 100)
            fim = self._le_float(self.var_var_fim, 'comando final (%)', 0, 100)
            passo = self._le_float(self.var_var_passo, 'passo (%)', 1e-6, 100)
            perm = self._le_float(self.var_var_perm, 'permanencia por patamar (s)', 1e-6)
            # como na escada, a janela de media tem de caber dentro do patamar:
            # acima disso ela so mediria o patamar inteiro, transitorio incluso.
            media = self._le_float(self.var_var_media, 'media dos ultimos (s)', 1e-6, perm)
        except ValueError as erro:
            messagebox.showerror('Parametro invalido', str(erro))
            return
        if fim <= ini:
            messagebox.showerror('Parametro invalido',
                                 'O comando final tem de ser maior que o inicial.')
            return

        subida = []
        u = ini
        while u < fim - 1e-9:
            subida.append(round(u, 6))
            u += passo
        subida.append(fim)
        descida = list(reversed(subida[:-1]))
        lista = [(u, 'subida') for u in subida] + [(u, 'descida') for u in descida]

        if not self.app.confirma_calibracao_lt('varredura estatica'):
            return

        # Onde salvar e escolhido ANTES de tomar o controle da planta: assim
        # cancelar o dialogo nao deixa a bomba reservada por um ensaio que nao
        # vai acontecer. O dialogo tambem ja cuida da confirmacao de
        # sobrescrita, entao nao ha o que checar aqui.
        sugestao = self.var_var_arquivo.get().strip() or ARQUIVO_VARREDURA
        caminho = filedialog.asksaveasfilename(
            title='Salvar dados consolidados da varredura estatica',
            defaultextension='.csv', initialfile=os.path.basename(sugestao),
            initialdir=os.path.dirname(os.path.abspath(sugestao)),
            filetypes=[('CSV', '*.csv'), ('todos os arquivos', '*.*')])
        if not caminho:
            return
        # Alguns Tk/macOS nao aplicam `defaultextension` de forma confiavel -
        # mesmo cuidado do botao de exportar grafico da Aula 1.
        if not os.path.splitext(caminho)[1]:
            caminho += '.csv'
        self.var_var_arquivo.set(caminho)

        if not self.app.pede_controle('Aula 3 - varredura estatica'):
            return

        try:
            self._var_arquivo = open(caminho, 'w', newline='')
        except OSError as erro:
            self.app.libera_controle()
            messagebox.showerror('Nao foi possivel gravar',
                                 f'{caminho}\n\n{erro}')
            return
        self._var_escritor = csv.writer(self._var_arquivo)
        self._var_escritor.writerow(['u_pct', 'sentido', 'qin_lpm', 'h_mm'])
        for item in self.tabela_var.get_children():
            self.tabela_var.delete(item)
        self._var_pontos = []
        self.bt_var_graf.configure(state='disabled')

        self._var_estado = {
            'lista': lista, 'idx': 0, 't0': None, 't_inicio_patamar': 0.0,
            'permanencia': perm, 'media_s': media, 'buffer': deque(maxlen=4000),
        }
        self.app.aplica_comando(100.0, lista[0][0])
        self.bt_var.configure(text='parar varredura')
        self.lb_var.configure(text=f'patamar 1/{len(lista)}: PUMP2 -> {lista[0][0]:.0f} % (subida)')

    def _salva_grafico_var(self):
        """Grafico da curva estatica do atuador: qin contra u, nos dois sentidos.

        E a figura do item 1 da analise da Aula 3. Sai com subida e descida
        como series separadas de proposito: a comparacao entre as duas e o
        que responde se ha histerese no atuador, e sobrepor tudo numa curva
        so apagaria justamente essa informacao.

        Le de `self._var_pontos`, nao do CSV: assim funciona tambem com a
        varredura ainda em andamento (util para conferir a zona morta sem
        esperar a descida inteira) e nao depende de o arquivo ainda estar
        aberto.
        """
        if not self._var_pontos:
            messagebox.showinfo(
                'Nada a desenhar',
                'Nenhum patamar foi medido ainda. Rode a varredura estatica '
                'primeiro - o botao se habilita assim que o primeiro patamar '
                'e gravado.')
            return

        base = os.path.splitext(os.path.basename(
            self.var_var_arquivo.get().strip() or ARQUIVO_VARREDURA))[0]
        caminho = filedialog.asksaveasfilename(
            title='Salvar grafico da curva estatica do atuador (qin x u)',
            defaultextension='.pdf', initialfile=f'{base}.pdf',
            filetypes=[('PDF', '*.pdf'), ('PNG', '*.png')])
        if not caminho:
            return
        # Mesmo cuidado dos demais botoes de exportar: alguns Tk/macOS nao
        # aplicam `defaultextension` de forma confiavel.
        if not os.path.splitext(caminho)[1]:
            caminho += '.pdf'

        erro = self._desenha_curva_atuador(caminho)
        if erro:
            messagebox.showerror('Nao foi possivel gerar o grafico', erro)
            return
        n_sub = sum(1 for _u, sent, _q, _h in self._var_pontos if sent == 'subida')
        n_des = len(self._var_pontos) - n_sub
        self.lb_var.configure(
            text=f'grafico salvo em {caminho} '
                 f'({n_sub} patamares de subida, {n_des} de descida).')

    def _desenha_curva_atuador(self, caminho):
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
        except ImportError as erro:
            return (f'Nao foi possivel importar matplotlib para gerar o grafico:\n{erro}')

        estilos = (('subida', 'o-', 'subida'), ('descida', 's--', 'descida'))
        fig, ax = plt.subplots(figsize=(7, 4.5))
        for sentido, estilo, rotulo in estilos:
            pontos = [(u, q) for u, sent, q, _h in self._var_pontos if sent == sentido]
            if not pontos:
                continue
            ax.plot([u for u, _q in pontos], [q for _u, q in pontos], estilo,
                    markersize=4, linewidth=1.2, label=rotulo)
        ax.set_xlabel('$u$  (comando de PUMP2)  [%]')
        ax.set_ylabel('$q_{in}$  (FT2)  [L/min]')
        ax.set_title('Curva estatica do atuador')
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=9)

        try:
            fig.tight_layout()
            fig.savefig(caminho, dpi=150)
        except Exception as erro:
            return str(erro)
        finally:
            plt.close(fig)
        return None

    def _encerra_var(self, motivo):
        n = len(self.tabela_var.get_children())
        caminho = self._var_arquivo.name if self._var_arquivo else ''
        if self._var_arquivo is not None:
            self._var_arquivo.close()
        self._var_arquivo = None
        self._var_escritor = None
        self._var_estado = None
        self.app.libera_controle()
        self.bt_var.configure(text='iniciar varredura')
        self.lb_var.configure(text=f'parado ({motivo}). {n} patamares salvos em {caminho}.')

    def _atualiza_var(self, t, valores):
        estado = self._var_estado
        if estado['t0'] is None:
            estado['t0'] = t
        trel = t - estado['t0']
        qin = contas_para_vazao(valores['FT2'])
        h = self.app.contas_para_altura_ativa(valores['LT'])
        estado['buffer'].append((trel, qin, h))

        if trel - estado['t_inicio_patamar'] >= estado['permanencia']:
            qin_medio = self._janela_media([(tt, qq) for tt, qq, _h in estado['buffer']],
                                           trel, estado['media_s'])
            h_medio = self._janela_media([(tt, hh) for tt, _q, hh in estado['buffer']],
                                         trel, estado['media_s'])
            idx = estado['idx']
            u_atual, sentido_atual = estado['lista'][idx]
            self._var_escritor.writerow([f'{u_atual:.1f}', sentido_atual,
                                         f'{qin_medio:.4f}', f'{h_medio:.2f}'])
            self._var_arquivo.flush()
            # `:.1f` no comando, e nao `:.0f`: a varredura fina da zona morta
            # que o roteiro pede anda de 2,5 em 2,5 %, e arredondar para
            # inteiro colapsaria patamares distintos na mesma linha.
            linha = self.tabela_var.insert(
                '', 'end', values=(f'{u_atual:.1f}', sentido_atual,
                                   f'{qin_medio:.3f}', f'{h_medio:.1f}'))
            self.tabela_var.see(linha)
            self._var_pontos.append((u_atual, sentido_atual, qin_medio, h_medio))
            self.bt_var_graf.configure(state='normal')

            idx += 1
            if idx >= len(estado['lista']):
                self._encerra_var('varredura concluida')
                return
            estado['idx'] = idx
            estado['t_inicio_patamar'] = trel
            estado['buffer'].clear()
            u_novo, _sentido_novo = estado['lista'][idx]
            self.app.aplica_comando(100.0, u_novo)

        self._mostra_progresso_var(trel, qin, h)

    def _mostra_progresso_var(self, trel, qin, h):
        """Escreve o progresso da varredura no rotulo da aba e na barra de status.

        Os dois textos saem do mesmo ponto, DEPOIS da eventual troca de
        patamar, para que nenhum deles passe meio segundo anunciando o
        patamar anterior - `aplica_comando`, na troca, repoe o texto generico
        de controle automatico na barra, e e esta chamada que o substitui.
        """
        estado = self._var_estado
        idx = estado['idx']
        u_atual, sentido = estado['lista'][idx]
        decorrido = trel - estado['t_inicio_patamar']
        passo = f'patamar {idx + 1}/{len(estado["lista"])}'
        self.lb_var.configure(
            text=f'{passo}: PUMP2 = {u_atual:.0f} % ({sentido}) | faltam '
                 f'{self._mmss(estado["permanencia"] - decorrido)} | '
                 f'qin = {qin:.2f} L/min | h = {h:.1f} mm')
        self.app.status_ensaio(
            f'{passo} - PUMP2 {u_atual:.0f} % ({sentido})   |   decorrido '
            f'{decorrido:.0f} s de {estado["permanencia"]:.0f} s neste patamar   |   '
            f'qin {qin:5.2f} L/min   |   h {h:6.1f} mm')

    # -- escada de degraus -------------------------------------------------

    def _alterna_esc(self):
        if self._esc_estado is None:
            self._inicia_esc()
        else:
            self._encerra_esc('interrompida pelo usuario')

    def _inicia_esc(self):
        seq_txt = self.var_esc_seq.get().strip()
        try:
            seq = [float(v.replace(',', '.')) for v in seq_txt.split(',') if v.strip()]
        except ValueError:
            messagebox.showerror('Sequencia invalida',
                                 'Digite comandos separados por virgula, ex.: 50,70,85,70,50.')
            return
        if len(seq) < 2:
            messagebox.showerror('Sequencia invalida',
                                 'A escada precisa de pelo menos dois patamares.')
            return
        if any(u < 0 or u > 100 for u in seq):
            messagebox.showerror('Sequencia invalida', 'Cada comando tem de estar entre 0 e 100.')
            return
        try:
            dur = self._le_float(self.var_esc_dur, 'duracao por patamar (s)', 1e-6)
            T = self._le_float(self.var_esc_T, 'periodo T (s)', 1e-6)
            media = self._le_float(self.var_esc_media, 'media dos ultimos (s)', 1e-6, dur)
        except ValueError as erro:
            messagebox.showerror('Parametro invalido', str(erro))
            return

        # O roteiro da Aula 3 recorta cada degrau ("exportar dados") DEPOIS que a
        # escada inteira termina - o que so funciona enquanto o ensaio couber no
        # historico que o grafico mantem em memoria (JANELA_MAX_S). A escada
        # padrao (5 x 600 s = 50 min) cabe com folga; avisar aqui evita perder os
        # primeiros degraus so no fim de quase uma hora de bancada.
        total_s = len(seq) * dur
        if total_s > JANELA_MAX_S:
            if not messagebox.askyesno(
                    'Escada mais longa que o historico',
                    f'Esta escada vai durar {total_s / 60:.0f} min ({len(seq)} patamares '
                    f'de {dur / 60:.1f} min), mais que os {JANELA_MAX_S / 60:.0f} min de '
                    'historico que o grafico mantem em memoria.\n\n'
                    'O CSV continuo sai completo, mas os primeiros degraus ja terao saido '
                    'do grafico quando a escada acabar - e e do grafico que sai o recorte '
                    'de cada degrau ("exportar dados").\n\n'
                    'Reduza a duracao por patamar, ou recorte os degraus iniciais ao longo '
                    'do ensaio, sem esperar o fim.\n\nComecar assim mesmo?'):
                return

        if not self.app.confirma_calibracao_lt('escada de degraus'):
            return

        # Como na varredura: onde salvar e escolhido ANTES de tomar o controle
        # da planta, para que cancelar o dialogo nao deixe a bomba reservada
        # por um ensaio que nao vai acontecer. O dialogo ja cuida da
        # confirmacao de sobrescrita do CSV continuo; o arquivo de
        # equilibrios e derivado dele e sobrescrito junto.
        caminho = filedialog.asksaveasfilename(
            title='Salvar CSV continuo da escada de degraus',
            defaultextension='.csv', initialfile=os.path.basename(ARQUIVO_ESCADA),
            initialdir=os.path.dirname(os.path.abspath(ARQUIVO_ESCADA)),
            filetypes=[('CSV', '*.csv'), ('todos os arquivos', '*.*')])
        if not caminho:
            return
        # Alguns Tk/macOS nao aplicam `defaultextension` de forma confiavel -
        # mesmo cuidado da varredura e do botao de exportar grafico da Aula 1.
        if not os.path.splitext(caminho)[1]:
            caminho += '.csv'
        base, _ext = os.path.splitext(caminho)
        caminho_eq = base + '_equilibrios.csv'

        if not self.app.pede_controle('Aula 3 - escada de degraus'):
            return

        self._esc_gravador = GravadorEnsaio(
            caminho, T, pump2_pct_fn=lambda: self._esc_estado['seq'][self._esc_estado['idx']],
            h_fn=self.app.contas_para_altura_ativa)
        self._esc_arquivo_eq = open(caminho_eq, 'w', newline='')
        self._esc_escritor_eq = csv.writer(self._esc_arquivo_eq)
        self._esc_escritor_eq.writerow(
            ['patamar', 'u_pct', 't_inicio_s', 'h_eq_mm', 'qin_eq_lpm', 'deriva_mm'])
        for item in self.tabela_esc.get_children():
            self.tabela_esc.delete(item)

        self._esc_estado = {
            'seq': seq, 'idx': 0, 'dur': dur, 'media_s': media,
            't_inicio_patamar': 0.0, 'buffer': deque(maxlen=8000),
        }
        self.app.aplica_comando(100.0, seq[0])
        self.bt_esc.configure(text='parar escada')
        self.lb_esc.configure(text=f'patamar 1/{len(seq)}: PUMP2 -> {seq[0]:.0f} %')

    def _encerra_esc(self, motivo):
        n = self._esc_gravador.linhas if self._esc_gravador else 0
        caminho = self._esc_gravador.caminho if self._esc_gravador else ''
        if self._esc_gravador is not None:
            self._esc_gravador.fecha()
        if self._esc_arquivo_eq is not None:
            self._esc_arquivo_eq.close()
        self._esc_gravador = None
        self._esc_arquivo_eq = None
        self._esc_escritor_eq = None
        self._esc_estado = None
        self.app.libera_controle()
        self.bt_esc.configure(text='iniciar escada')
        self.lb_esc.configure(text=f'parado ({motivo}). {n} amostras salvas em {caminho}.')

    def _atualiza_esc(self, t, valores):
        estado = self._esc_estado
        trel = self._esc_gravador.recebe(t, valores)
        # Mesma conversao do CSV continuo (ver `GravadorEnsaio`): o h_eq_mm do
        # arquivo de equilibrios tem de sair da calibracao ativa, senao as duas
        # metades do mesmo ensaio ficariam em escalas diferentes.
        h = self.app.contas_para_altura_ativa(valores['LT'])
        qin = contas_para_vazao(valores['FT2'])
        estado['buffer'].append((trel, h, qin))

        idx_atual = estado['idx']
        self.lb_esc.configure(
            text=f'patamar {idx_atual + 1}/{len(estado["seq"])}: PUMP2 = '
                 f'{estado["seq"][idx_atual]:.0f} % desde t = '
                 f'{estado["t_inicio_patamar"]:.0f} s | faltam '
                 f'{self._mmss(estado["dur"] - (trel - estado["t_inicio_patamar"]))} | '
                 f'h = {h:.1f} mm (deriva {self._deriva_h(estado["buffer"], trel):+.1f} mm '
                 f'em {self.DERIVA_JANELA_S:.0f} s) | qin = {qin:.2f} L/min')

        if trel - estado['t_inicio_patamar'] >= estado['dur']:
            h_eq = self._janela_media([(tt, hh) for tt, hh, _q in estado['buffer']],
                                      trel, estado['media_s'])
            qin_eq = self._janela_media([(tt, qq) for tt, _h, qq in estado['buffer']],
                                        trel, estado['media_s'])
            deriva = self._deriva_h(estado['buffer'], trel)
            idx = estado['idx']
            self._esc_escritor_eq.writerow([
                idx + 1, f'{estado["seq"][idx]:.1f}', f'{estado["t_inicio_patamar"]:.2f}',
                f'{h_eq:.2f}', f'{qin_eq:.4f}', f'{deriva:.2f}',
            ])
            self._esc_arquivo_eq.flush()
            # "uns poucos milimetros" do roteiro: acima de 3 mm o patamar ainda
            # esta andando, e tanto o h_eq quanto o delta_h_inf do degrau
            # seguinte saem contaminados.
            tags = ('instavel',) if abs(deriva) > 3.0 else ()
            linha = self.tabela_esc.insert('', 'end', tags=tags, values=(
                idx + 1, f'{estado["seq"][idx]:.0f}', f'{estado["t_inicio_patamar"]:.0f}',
                f'{h_eq:.1f}', f'{qin_eq:.3f}', f'{deriva:+.1f}'))
            self.tabela_esc.see(linha)

            idx += 1
            if idx >= len(estado['seq']):
                self._encerra_esc('sequencia concluida')
                return
            estado['idx'] = idx
            estado['t_inicio_patamar'] = trel
            estado['buffer'].clear()
            u_novo = estado['seq'][idx]
            self.app.aplica_comando(100.0, u_novo)
            # o rotulo em si e reescrito na proxima amostra, com o tempo restante

    # -- despacho ------------------------------------------------------

    def atualiza_amostra(self, t, valores):
        if self._var_estado is not None:
            self._atualiza_var(t, valores)
        if self._esc_estado is not None and self._esc_gravador is not None:
            self._atualiza_esc(t, valores)


class PIDiscreto:
    """PI em forma de posicao, com saturacao e anti-windup opcional.

    E o MESMO codigo do cod:pi-discreto da Aula 4 (conteudo.tex, secao
    pi-discreto): a aba roda exatamente esta classe, e nao uma reimplementacao
    paralela, para que o item pi-analise-implementacao possa recalcular os
    comandos do CSV gravado e bater ponto a ponto com o que a bancada fez.
    """

    def __init__(self, Kc, Ti, T, u_min=45.0, u_max=100.0, anti_windup=True):
        self.Kc, self.Ti, self.T = Kc, Ti, T       # Ti = None: proporcional puro
        self.u_min, self.u_max = u_min, u_max
        self.anti_windup = anti_windup
        self.I = 0.0                                 # termo integral, em %

    def inicia(self, u_atual, e_atual):
        """Partida sem solavanco; no P puro, chame inicia(u_b, 0.0)."""
        self.I = u_atual - self.Kc * e_atual

    def calcula(self, r, h):
        e = r - h
        u = self.Kc * e + self.I                     # comando pedido
        uc = min(max(u, self.u_min), self.u_max)     # comando aplicado
        agrava = (u > self.u_max and e > 0) or (u < self.u_min and e < 0)
        if self.Ti is not None and not (self.anti_windup and agrava):
            self.I += self.Kc * self.T / self.Ti * e # Euler progressivo
        return uc, u


class AbaAula4(AbaBase):
    """Aula 4 - malha fechada com PI discreto, saturacao e anti-windup (Secao pi-hub)."""

    # Janela de media para o h_final_mm do CSV de patamares - mesma ideia da
    # "media dos ultimos 10 s" do roteiro (Secao pi-hub, Tab. de patamares).
    MEDIA_H_FINAL_S = 10.0

    def __init__(self, master, app):
        super().__init__(master, app)
        self._estado = None
        self._pi = None
        self._arquivo = None
        self._escritor = None
        self._arquivo_pat = None
        self._escritor_pat = None
        self._monta()

    def _monta(self):
        bloco = ttk.LabelFrame(self, text='Malha fechada (Secao pi-hub)', padding=10)
        bloco.pack(fill='both', expand=True)

        ttk.Label(
            bloco, text='Fecha a malha de nivel com o PI discreto do Cod. pi-discreto: mesma\n'
                        'saturacao, mesmo anti-windup por integracao condicional (opcional) e\n'
                        'mesma partida sem solavanco do roteiro. Exige VALVE em pelo menos 30 %\n'
                        '(slider/botao do topo da janela). Por seguranca, a malha abre e\n'
                        f'desliga PUMP2 se h passar de {MALHA_H_DESARME_MM:.0f} mm.',
            justify='left').grid(row=0, column=0, columnspan=6, sticky='w', pady=(0, 10))

        # -- controlador ----------------------------------------------------
        quad_ctrl = ttk.LabelFrame(bloco, text='Controlador', padding=8)
        quad_ctrl.grid(row=1, column=0, columnspan=6, sticky='we', pady=(0, 10))

        self.var_ai = tk.BooleanVar(value=True)
        ttk.Checkbutton(quad_ctrl, text='acao integral', variable=self.var_ai,
                       command=self._atualiza_campos_ctrl).grid(
            row=0, column=0, columnspan=2, sticky='w')

        # Valores-exemplo do projeto da Secao pi-projeto (K = 3,2 mm/%,
        # tau = 130 s, zeta = 0,8, ts = 150 s): ajudam o aluno a conferir que
        # digitou os proprios ganhos no campo certo, mas cada grupo digita os
        # ganhos do SEU projeto (Tab. pi-projeto), nao estes.
        campos_ctrl = (
            ('Kc [%/mm]', 'var_kc', '1.85'),
            ('Ti [s]', 'var_ti', '41'),
            ('u_b [%] (P puro)', 'var_ub', '50'),
            ('T [s]', 'var_T', '2'),
            ('u_min [%]', 'var_umin', '45'),
            ('u_max [%]', 'var_umax', '100'),
        )
        self._entradas_ctrl = {}
        for i, (rotulo, nome, padrao) in enumerate(campos_ctrl):
            v = tk.StringVar(value=padrao)
            setattr(self, nome, v)
            ttk.Label(quad_ctrl, text=rotulo + ':').grid(
                row=1 + i // 3, column=2 * (i % 3), sticky='w', pady=(4, 0))
            entrada = ttk.Entry(quad_ctrl, textvariable=v, width=8)
            entrada.grid(row=1 + i // 3, column=2 * (i % 3) + 1, sticky='w',
                        padx=(4, 20), pady=(4, 0))
            self._entradas_ctrl[nome] = entrada

        ttk.Label(quad_ctrl, text='anti-windup:').grid(row=3, column=0, sticky='w', pady=(6, 0))
        self.var_aw = tk.StringVar(value='integração condicional')
        ttk.Combobox(quad_ctrl, textvariable=self.var_aw, state='readonly', width=22,
                    values=('nenhum', 'integração condicional')).grid(
            row=3, column=1, columnspan=2, sticky='w', padx=(4, 0), pady=(6, 0))

        self._atualiza_campos_ctrl()

        # -- referencia -------------------------------------------------
        quad_ref = ttk.LabelFrame(bloco, text='Referencia', padding=8)
        quad_ref.grid(row=2, column=0, columnspan=6, sticky='we', pady=(0, 10))
        ttk.Label(quad_ref, text='sequencia de referencias (mm):').grid(row=0, column=0, sticky='w')
        self.var_ref_seq = tk.StringVar(value='70,80,125,70')
        ttk.Entry(quad_ref, textvariable=self.var_ref_seq, width=28).grid(
            row=0, column=1, columnspan=3, sticky='w', padx=(4, 0))
        ttk.Label(quad_ref, text='duracao por patamar (s):').grid(
            row=1, column=0, sticky='w', pady=(6, 0))
        self.var_ref_dur = tk.StringVar(value='300')
        ttk.Entry(quad_ref, textvariable=self.var_ref_dur, width=8).grid(
            row=1, column=1, sticky='w', padx=(4, 0), pady=(6, 0))
        ttk.Label(
            quad_ref, text=f'cada referencia tem de estar entre {MALHA_REF_MIN_MM:.0f} e '
                          f'{MALHA_REF_MAX_MM:.0f} mm.',
            foreground='#555', font=('TkDefaultFont', 8)).grid(
            row=2, column=0, columnspan=4, sticky='w', pady=(4, 0))

        # -- iniciar/parar e tabela ao vivo ----------------------------------
        self.bt_malha = ttk.Button(bloco, text='iniciar malha', command=self._alterna_malha)
        self.bt_malha.grid(row=3, column=0, columnspan=2, sticky='w', pady=(4, 0))
        self.lb_malha = ttk.Label(bloco, text='parado.')
        self.lb_malha.grid(row=3, column=2, columnspan=4, sticky='w', pady=(4, 0))

        quadro_tab = ttk.Frame(bloco)
        quadro_tab.grid(row=4, column=0, columnspan=6, sticky='nsew', pady=(8, 0))
        bloco.rowconfigure(4, weight=1)
        bloco.columnconfigure(5, weight=1)
        quadro_tab.rowconfigure(0, weight=1)
        quadro_tab.columnconfigure(0, weight=1)
        self.tabela = ttk.Treeview(
            quadro_tab, columns=('patamar', 'r', 't0', 'hext', 'hfinal', 'tsat'),
            show='headings', height=6)
        for coluna, texto in (('patamar', 'patamar'), ('r', 'r [mm]'), ('t0', 't inicio [s]'),
                              ('hext', 'h extremo [mm]'), ('hfinal', 'h final [mm]'),
                              ('tsat', 't saturado [s]')):
            self.tabela.heading(coluna, text=texto)
        barra = ttk.Scrollbar(quadro_tab, orient='vertical', command=self.tabela.yview)
        self.tabela.configure(yscrollcommand=barra.set)
        self.tabela.grid(row=0, column=0, sticky='nsew')
        barra.grid(row=0, column=1, sticky='ns')

    def _atualiza_campos_ctrl(self):
        """Habilita Ti (PI) ou u_b (P puro) conforme a caixa 'acao integral' -
        a mesma dicotomia do Cod. pi-discreto (Ti=None vira proporcional
        puro, com o comando de base fixo somado no lugar do integrador)."""
        if self.var_ai.get():
            self._entradas_ctrl['var_ti'].configure(state='normal')
            self._entradas_ctrl['var_ub'].configure(state='disabled')
        else:
            self._entradas_ctrl['var_ti'].configure(state='disabled')
            self._entradas_ctrl['var_ub'].configure(state='normal')

    # -- helpers ---------------------------------------------------------

    def _le_float(self, var, nome, minimo=None, maximo=None):
        try:
            valor = float(var.get().replace(',', '.'))
        except ValueError:
            raise ValueError(f'{nome} precisa ser um numero.')
        if minimo is not None and valor < minimo:
            raise ValueError(f'{nome} tem de ser >= {minimo}.')
        if maximo is not None and valor > maximo:
            raise ValueError(f'{nome} tem de ser <= {maximo}.')
        return valor

    @staticmethod
    def _mmss(segundos):
        segundos = max(0, int(round(segundos)))
        return f'{segundos // 60:d}:{segundos % 60:02d}'

    def _janela_media(self, amostras, trel, media_s):
        vistos = [v for tt, v in amostras if trel - tt <= media_s]
        return sum(vistos) / len(vistos) if vistos else float('nan')

    # -- iniciar/parar a malha -------------------------------------------

    def _alterna_malha(self):
        if self._estado is None:
            self._inicia_malha()
        else:
            self._encerra_malha('interrompida pelo usuario')

    def _inicia_malha(self):
        try:
            Kc = self._le_float(self.var_kc, 'Kc')
            T = self._le_float(self.var_T, 'T', 1e-6)
            u_min = self._le_float(self.var_umin, 'u_min', 0, 100)
            u_max = self._le_float(self.var_umax, 'u_max', 0, 100)
            ai = self.var_ai.get()
            if ai:
                Ti = self._le_float(self.var_ti, 'Ti', 1e-6)
                u_b = None
            else:
                Ti = None
                u_b = self._le_float(self.var_ub, 'u_b', 0, 100)
        except ValueError as erro:
            messagebox.showerror('Parametro invalido', str(erro))
            return
        if u_max <= u_min:
            messagebox.showerror('Parametro invalido', 'u_max tem de ser maior que u_min.')
            return

        seq_txt = self.var_ref_seq.get().strip()
        try:
            seq = [float(v.replace(',', '.')) for v in seq_txt.split(',') if v.strip()]
        except ValueError:
            messagebox.showerror(
                'Sequencia invalida',
                'Digite referencias separadas por virgula, em mm, ex.: 70,80,125,70.')
            return
        if not seq:
            messagebox.showerror('Sequencia invalida',
                                 'A referencia precisa de pelo menos um patamar.')
            return
        if any(r < MALHA_REF_MIN_MM or r > MALHA_REF_MAX_MM for r in seq):
            messagebox.showerror(
                'Sequencia invalida',
                f'Cada referencia tem de estar entre {MALHA_REF_MIN_MM:.0f} e '
                f'{MALHA_REF_MAX_MM:.0f} mm.')
            return
        try:
            dur = self._le_float(self.var_ref_dur, 'duracao por patamar (s)', 1e-6)
        except ValueError as erro:
            messagebox.showerror('Parametro invalido', str(erro))
            return

        # Mesmo aviso da escada da Aula 3: sequencias longas podem ultrapassar
        # o historico que o grafico mantem em memoria (JANELA_MAX_S).
        total_s = len(seq) * dur
        if total_s > JANELA_MAX_S:
            if not messagebox.askyesno(
                    'Malha mais longa que o historico',
                    f'Esta sequencia vai durar {total_s / 60:.0f} min ({len(seq)} patamares '
                    f'de {dur / 60:.1f} min), mais que os {JANELA_MAX_S / 60:.0f} min de '
                    'historico que o grafico mantem em memoria.\n\n'
                    'O CSV continuo sai completo, mas o inicio do ensaio ja tera saido do '
                    'grafico quando a malha acabar - e e do grafico que sai o recorte de '
                    '"exportar dados".\n\nComecar assim mesmo?'):
                return

        if not self.app.confirma_calibracao_lt('malha fechada'):
            return
        if 'LT' not in self.app.painel_leituras.ultimas:
            messagebox.showwarning('Sem leitura', 'Ainda nao ha leitura de LT. Aguarde a conexao.')
            return

        sugestao = ARQUIVO_MALHA
        caminho = filedialog.asksaveasfilename(
            title='Salvar CSV continuo da malha fechada',
            defaultextension='.csv', initialfile=os.path.basename(sugestao),
            filetypes=[('CSV', '*.csv'), ('todos os arquivos', '*.*')])
        if not caminho:
            return
        # Mesmo cuidado das demais abas: alguns Tk/macOS nao aplicam
        # `defaultextension` de forma confiavel.
        if not os.path.splitext(caminho)[1]:
            caminho += '.csv'
        base, _ext = os.path.splitext(caminho)
        caminho_pat = base + '_patamares.csv'

        if not self.app.pede_controle('Aula 4 - malha fechada'):
            return

        h_atual = self.app.contas_para_altura_ativa(self.app.painel_leituras.ultimas['LT'])
        u_atual = self.app.pump2_pct

        pi = PIDiscreto(Kc, Ti, T, u_min=u_min, u_max=u_max,
                        anti_windup=(self.var_aw.get() == 'integração condicional'))
        if ai:
            pi.inicia(u_atual, seq[0] - h_atual)          # partida sem solavanco
        else:
            pi.inicia(u_b, 0.0)                            # u = u_b + Kc*e sempre

        try:
            self._arquivo = open(caminho, 'w', newline='')
        except OSError as erro:
            self.app.libera_controle()
            messagebox.showerror('Nao foi possivel gravar', f'{caminho}\n\n{erro}')
            return
        self._escritor = csv.writer(self._arquivo)
        self._escritor.writerow(
            ['t_s', 'r_mm', 'h_mm', 'e_mm', 'u_pct', 'uc_pct', 'i_pct',
             'valve_pct', 'pump2_pct', 'qin_lpm'])
        self._arquivo_pat = open(caminho_pat, 'w', newline='')
        self._escritor_pat = csv.writer(self._arquivo_pat)
        self._escritor_pat.writerow(
            ['patamar', 'r_mm', 't_inicio_s', 'h_ext_mm', 'h_final_mm', 't_sat_s'])
        for item in self.tabela.get_children():
            self.tabela.delete(item)

        self.app.gr.limpa_referencia()
        self.app.gr.limpa_ctrl()
        self.app.mostra_curvas_controlador(True)
        self._pi = pi
        self._estado = {
            'seq': seq, 'idx': 0, 'dur': dur, 'T': T,
            't0': None, 't_ultimo_calc': None, 't_inicio_patamar': 0.0,
            'buffer_h': deque(maxlen=8000), 't_sat': 0.0,
            'h_ext': seq[0], 'sentido': None,
        }
        # Reaplica o comando ja em vigor (sem pulo nenhum): o primeiro comando
        # de verdade do controlador so sai no proximo `atualiza_amostra`, e a
        # partida sem solavanco garante que ele coincide com este.
        self.app.aplica_comando(100.0, u_atual)
        self.app.define_valve_livre(True)             # slider de VALVE = disturbio
        self.bt_malha.configure(text='parar malha')
        self.lb_malha.configure(text=f'patamar 1/{len(seq)}: r -> {seq[0]:.0f} mm')

    def _encerra_malha(self, motivo, desliga_bomba=False):
        caminho = self._arquivo.name if self._arquivo else ''
        if self._arquivo is not None:
            self._arquivo.close()
        if self._arquivo_pat is not None:
            self._arquivo_pat.close()
        self._arquivo = None
        self._escritor = None
        self._arquivo_pat = None
        self._escritor_pat = None
        self._estado = None
        self._pi = None
        self.app.define_valve_livre(False)
        if desliga_bomba:
            self.app.aplica_comando(100.0, 0.0)
        elif self.app.valve_pct < 100.0 - 1e-6:
            # devolve S a 100 % (intertravamento de volta) mantendo a bomba
            self.app.aplica_comando(100.0, self.app.pump2_pct)
        self.app.mostra_curvas_controlador(False)
        self.app.libera_controle()
        self.bt_malha.configure(text='iniciar malha')
        self.lb_malha.configure(text=f'parado ({motivo}). dados em {caminho}.')

    # -- laco de controle --------------------------------------------------

    def _atualiza_malha(self, t, valores):
        estado = self._estado
        if estado['t0'] is None:
            estado['t0'] = t
            estado['t_ultimo_calc'] = -estado['T']    # forca o 1o calculo nesta amostra
        trel = t - estado['t0']
        h = self.app.contas_para_altura_ativa(valores['LT'])
        qin = contas_para_vazao(valores['FT2'])

        if h > MALHA_H_DESARME_MM:
            self._encerra_malha(
                f'desarme de seguranca: h = {h:.0f} mm > {MALHA_H_DESARME_MM:.0f} mm',
                desliga_bomba=True)
            messagebox.showwarning(
                'Desarme de seguranca',
                f'A malha foi aberta e PUMP2 foi desligada: o nivel passou de '
                f'{MALHA_H_DESARME_MM:.0f} mm.')
            return

        if trel - estado['t_ultimo_calc'] < estado['T'] - 1e-6:
            return
        estado['t_ultimo_calc'] = trel

        idx = estado['idx']
        seq = estado['seq']
        r = seq[idx]
        i_usado = self._pi.I                          # I[k], o que ENTROU no calculo de u[k]
        uc, u = self._pi.calcula(r, h)
        # O disturbio e a VALVE, movida pelo aluno no slider: a malha so a le.
        valve = self.app.valve_pct
        self.app.aplica_comando(valve, uc)

        e = r - h
        self._escritor.writerow([
            f'{trel:.3f}', f'{r:.2f}', f'{h:.3f}', f'{e:.3f}', f'{u:.2f}',
            f'{uc:.2f}', f'{i_usado:.2f}', f'{valve:.2f}', f'{uc:.2f}', f'{qin:.4f}',
        ])
        self._arquivo.flush()

        if abs(uc - u) > 1e-6:
            estado['t_sat'] += estado['T']

        estado['buffer_h'].append((trel, h))
        if estado['sentido'] is None:
            # Direcao do patamar: h no primeiro calculo dele, contra a nova
            # referencia - e por isso resetado a None a cada troca, abaixo.
            estado['sentido'] = 'sobe' if r >= h else 'desce'
            estado['h_ext'] = h
        elif estado['sentido'] == 'sobe':
            estado['h_ext'] = max(estado['h_ext'], h)
        else:
            estado['h_ext'] = min(estado['h_ext'], h)

        self.app.gr.acrescenta_referencia(t, r)
        self.app.gr.acrescenta(t, {'u': u, 'uc': uc, 'e': e,
                                   'uK': self._pi.Kc * e, 'uTi': i_usado})

        self.lb_malha.configure(
            text=f'patamar {idx + 1}/{len(seq)}: r = {r:.0f} mm | faltam '
                 f'{self._mmss(estado["dur"] - (trel - estado["t_inicio_patamar"]))} | '
                 f'h = {h:.1f} mm | e = {e:+.1f} mm | u_c = {uc:.1f} % '
                 f'(P {self._pi.Kc * e:+.1f} % | I {i_usado:+.1f} %) | u = {u:+.1f} %')
        self.app.status_ensaio(
            f'patamar {idx + 1}/{len(seq)} - r {r:.0f} mm   |   h {h:6.1f} mm   |   '
            f'u_c {uc:5.1f} % (P {self._pi.Kc * e:+6.1f} | I {i_usado:+6.1f})   |   '
            f'u {u:+6.1f} %')

        if trel - estado['t_inicio_patamar'] >= estado['dur']:
            h_final = self._janela_media(estado['buffer_h'], trel, self.MEDIA_H_FINAL_S)
            self._escritor_pat.writerow([
                idx + 1, f'{r:.1f}', f'{estado["t_inicio_patamar"]:.2f}',
                f'{estado["h_ext"]:.2f}', f'{h_final:.2f}', f'{estado["t_sat"]:.1f}',
            ])
            self._arquivo_pat.flush()
            linha = self.tabela.insert('', 'end', values=(
                idx + 1, f'{r:.0f}', f'{estado["t_inicio_patamar"]:.0f}',
                f'{estado["h_ext"]:.1f}', f'{h_final:.1f}', f'{estado["t_sat"]:.0f}'))
            self.tabela.see(linha)

            idx += 1
            if idx >= len(seq):
                self._encerra_malha('sequencia concluida')
                return
            estado['idx'] = idx
            estado['t_inicio_patamar'] = trel
            estado['buffer_h'].clear()
            estado['t_sat'] = 0.0
            estado['sentido'] = None
            # o rotulo em si e reescrito no proximo calculo, com o tempo restante

    # -- despacho ------------------------------------------------------

    def atualiza_amostra(self, t, valores):
        if self._estado is not None:
            self._atualiza_malha(t, valores)


class PIDLivre:
    """PID em forma paralela e de posicao, para o painel "Comando com
    Controlador PID": u = Kp*e + I + Kd*d/dt(-h filtrado).

    Diferencas em relacao ao `PIDiscreto` da Aula 4 (que segue literalmente o
    roteiro e nao muda): ganhos em forma paralela (Ki = Kc/Ti, Kd = Kc*Td),
    derivada sobre a MEDIDA (sem chute quando o set-point muda) passada por um
    filtro de primeira ordem de constante Tf, e os parametros sao atributos
    comuns, reatribuiveis a quente entre duas chamadas de `calcula`. Como o
    integrador acumula Ki*T*e (e nao Ki * soma de e), mexer em Ki nao da
    degrau no comando; mexer em Kp da, na proporcao Delta_Kp * e.
    """

    def __init__(self, Kp, Ki, Kd, T, Tf, u_min, u_max, anti_windup=True):
        self.Kp, self.Ki, self.Kd, self.T, self.Tf = Kp, Ki, Kd, T, Tf
        self.u_min, self.u_max = u_min, u_max
        self.anti_windup = anti_windup
        self.I = 0.0
        self.dh = 0.0                 # dh/dt filtrado, em mm/s
        self.h_ant = 0.0

    def inicia(self, u_atual, e_atual, h):
        """Partida sem solavanco: u[0] = u_atual (derivada ainda nula)."""
        self.I = u_atual - self.Kp * e_atual
        self.dh = 0.0
        self.h_ant = h

    def calcula(self, r, h):
        """Devolve (uc, u, P, I, D): I e o valor que ENTROU no calculo de u."""
        e = r - h
        a = self.Tf / (self.Tf + self.T)
        self.dh = a * self.dh + (1.0 - a) * (h - self.h_ant) / self.T
        self.h_ant = h
        P = self.Kp * e
        I = self.I
        D = -self.Kd * self.dh
        u = P + I + D
        uc = min(max(u, self.u_min), self.u_max)
        agrava = (u > self.u_max and e > 0) or (u < self.u_min and e < 0)
        if self.Ki != 0.0 and not (self.anti_windup and agrava):
            self.I += self.Ki * self.T * e
        return uc, u, P, I, D


class PainelPID(ttk.LabelFrame):
    """Comando com Controlador PID: fecha a malha de nivel "a vontade", fora de
    qualquer pratica. Ganhos, set-point e demais campos valem a quente - sao
    relidos a cada calculo do controlador."""

    def __init__(self, master, app, **kw):
        super().__init__(master, text='Comando com Controlador PID', **kw)
        self.app = app
        self._pid = None
        self._t_ult = None
        self._p = None               # ultimo conjunto de parametros valido
        # Registro dos parametros em vigor ao longo do tempo, para o CSV
        # exportado do grafico: lista de {'t_ini', 't_fim', 'p'} na mesma base
        # de tempo do historico da janela; 't_fim' None = segmento aberto.
        # Um segmento novo nasce a cada ativacao e a cada mudanca a quente.
        self.registro = []
        self._t_amostra = 0.0

        grupos = (
            ('Ganhos', (('Kp [%/mm]', 'Kp'), ('Ki [%/(mm.s)]', 'Ki'),
                        ('Kd [%.s/mm]', 'Kd'))),
            ('Parametros', (('T [s]', 'T'), ('Tf deriv. [s]', 'Tf'),
                            ('u_min [%]', 'umin'), ('u_max [%]', 'umax'))),
        )
        self.vars = {}
        for gcol, (titulo, campos) in enumerate(grupos):
            quad = ttk.LabelFrame(self, text=titulo, padding=(6, 2))
            quad.grid(row=0, column=gcol, sticky='ns', padx=(0, 6))
            for i, (rotulo, chave) in enumerate(campos):
                v = tk.StringVar(value=PID_PADRAO[chave])
                self.vars[chave] = v
                # parametros em 2 colunas de campos; ganhos em 1 coluna
                linha, col = divmod(i, 2) if titulo == 'Parametros' else (i, 0)
                ttk.Label(quad, text=rotulo + ':').grid(
                    row=linha, column=2 * col, sticky='w', pady=(0 if linha == 0 else 3, 0))
                ttk.Entry(quad, textvariable=v, width=7).grid(
                    row=linha, column=2 * col + 1, sticky='w', padx=(4, 10),
                    pady=(0 if linha == 0 else 3, 0))
            if titulo == 'Parametros':
                self.var_aw = tk.BooleanVar(value=True)
                ttk.Checkbutton(quad, text='anti-windup', variable=self.var_aw).grid(
                    row=2, column=0, columnspan=4, sticky='w', pady=(3, 0))

        # Set-point em separado, afastado dos ganhos/parametros por um
        # separador vertical.
        ttk.Separator(self, orient='vertical').grid(row=0, column=2, sticky='ns', padx=8)
        quad_sp = ttk.LabelFrame(self, text='Set-point', padding=(6, 2))
        quad_sp.grid(row=0, column=3, sticky='n')
        self.vars['sp'] = tk.StringVar(value=PID_PADRAO['sp'])
        ttk.Label(quad_sp, text='h [mm]:').grid(row=0, column=0, sticky='w')
        ttk.Entry(quad_sp, textvariable=self.vars['sp'], width=8,
                  font=('TkDefaultFont', 11, 'bold')).grid(row=0, column=1, padx=(4, 0))

        self.bt = ttk.Button(self, text='Ativar PID', command=self._alterna)
        self.bt.grid(row=1, column=0, sticky='w', pady=(8, 0))
        self.lb = ttk.Label(self, text='desativado.', foreground='#555',
                            font=('TkDefaultFont', 8), wraplength=420, justify='left')
        self.lb.grid(row=1, column=1, columnspan=3, sticky='w', padx=(4, 0), pady=(8, 0))

    # -- leitura dos campos --------------------------------------------

    def _le(self, chave, nome, minimo=None, maximo=None):
        try:
            valor = float(self.vars[chave].get().replace(',', '.'))
        except ValueError:
            raise ValueError(f'{nome} precisa ser um numero.')
        if minimo is not None and valor < minimo:
            raise ValueError(f'{nome} tem de ser >= {minimo:g}.')
        if maximo is not None and valor > maximo:
            raise ValueError(f'{nome} tem de ser <= {maximo:g}.')
        return valor

    # chave -> (nome, minimo, maximo)
    LIMITES = {
        'Kp': ('Kp', 0.0, None), 'Ki': ('Ki', 0.0, None), 'Kd': ('Kd', 0.0, None),
        'sp': ('set-point', MALHA_REF_MIN_MM, MALHA_REF_MAX_MM),
        'T': ('T', 0.5, None), 'Tf': ('Tf', 0.0, None),
        'umin': ('u_min', 0.0, 100.0), 'umax': ('u_max', 0.0, 100.0),
    }

    def _le_todos(self):
        p = {chave: self._le(chave, *lim) for chave, lim in self.LIMITES.items()}
        if p['umax'] <= p['umin']:
            raise ValueError('u_max tem de ser maior que u_min.')
        return p

    def _le_a_quente(self):
        """Como `_le_todos`, mas campo a campo: um campo invalido mantem o
        ultimo valor bom SO dele, sem travar os demais. Devolve (p, aviso)."""
        p, erros = dict(self._p), []
        for chave, lim in self.LIMITES.items():
            try:
                p[chave] = self._le(chave, *lim)
            except ValueError as erro:
                erros.append(str(erro))
        if p['umax'] <= p['umin']:
            p['umin'], p['umax'] = self._p['umin'], self._p['umax']
            erros.append('u_max tem de ser maior que u_min.')
        aviso = f'  [mantido o valor anterior: {" ".join(erros)}]' if erros else ''
        return p, aviso

    def _aplica_parametros(self, p):
        pid = self._pid
        pid.Kp, pid.Ki, pid.Kd, pid.T, pid.Tf = p['Kp'], p['Ki'], p['Kd'], p['T'], p['Tf']
        pid.u_min, pid.u_max = p['umin'], p['umax']
        pid.anti_windup = self.var_aw.get()

    # -- registro de parametros (para a exportacao) --------------------

    def _abre_segmento(self, t, p):
        self.registro.append({'t_ini': t, 't_fim': None,
                              'p': dict(p, aw=self.var_aw.get())})
        if self.app._historico:
            t_min = self.app._historico[0][0]
            self.registro = [g for g in self.registro
                             if g['t_fim'] is None or g['t_fim'] >= t_min]

    def _fecha_segmento(self, t):
        if self.registro and self.registro[-1]['t_fim'] is None:
            self.registro[-1]['t_fim'] = t

    def parametros_em(self, t):
        """Parametros do PID em vigor no instante `t` (base de tempo do
        historico), ou None se nao havia PID ativo."""
        for g in self.registro:
            if g['t_ini'] <= t and (g['t_fim'] is None or t < g['t_fim']):
                return g['p']
        return None

    def linha_exportacao(self, t):
        """Valores das COLUNAS_PID_EXPORTACAO para a amostra em `t`."""
        p = self.parametros_em(t)
        if p is None:
            return [0] + [''] * (len(COLUNAS_PID_EXPORTACAO) - 1)
        return [1, f'{p["Kp"]:g}', f'{p["Ki"]:g}', f'{p["Kd"]:g}', f'{p["sp"]:g}',
                f'{p["T"]:g}', f'{p["Tf"]:g}', f'{p["umin"]:g}', f'{p["umax"]:g}',
                int(p['aw'])]

    # -- ativar/desativar ----------------------------------------------

    def _alterna(self):
        if self._pid is None:
            self._ativa()
        else:
            self._desativa('desativado pelo usuario')

    def _ativa(self):
        try:
            p = self._le_todos()
        except ValueError as erro:
            messagebox.showerror('Parametro invalido', str(erro))
            return
        if 'LT' not in self.app.painel_leituras.ultimas:
            messagebox.showwarning('Sem leitura', 'Ainda nao ha leitura de LT. Aguarde a conexao.')
            return
        if not self.app.confirma_calibracao_lt('PID livre'):
            return
        if not self.app.pede_controle('PID livre'):
            return

        h = self.app.contas_para_altura_ativa(self.app.painel_leituras.ultimas['LT'])
        u_atual = self.app.pump2_pct
        self._pid = PIDLivre(p['Kp'], p['Ki'], p['Kd'], p['T'], p['Tf'],
                             p['umin'], p['umax'], self.var_aw.get())
        self._pid.inicia(u_atual, p['sp'] - h, h)
        self._t_ult = None
        self._p = p
        hist = self.app._historico
        self._t_amostra = hist[-1][0] if hist else 0.0
        self._abre_segmento(self._t_amostra + 1e-6, p)

        self.app.gr.limpa_referencia()
        self.app.gr.limpa_ctrl()
        self.app.mostra_curvas_controlador(True, com_derivativo=True)
        # Reaplica o comando em vigor com VALVE em 100 %; a partir daqui o
        # slider de VALVE fica livre (perturbacao) e o de PUMP2, bloqueado.
        self.app.aplica_comando(100.0, u_atual)
        self.app.define_valve_livre(True)
        self.bt.configure(text='Desativar PID')
        self.lb.configure(text='ativo. Ganhos e set-point valem a quente.', foreground='#a11')

    def _desativa(self, motivo, desliga_bomba=False):
        self._fecha_segmento(self._t_amostra + 1e-6)   # inclui a ultima amostra
        self._pid = None
        self.app.define_valve_livre(False)
        if desliga_bomba:
            self.app.aplica_comando(100.0, 0.0)
        elif self.app.valve_pct < 100.0 - 1e-6:
            self.app.aplica_comando(100.0, self.app.pump2_pct)
        self.app.mostra_curvas_controlador(False)
        self.app.libera_controle()
        self.bt.configure(text='Ativar PID')
        self.lb.configure(text=f'desativado ({motivo}).', foreground='#555')

    # -- laco de controle ----------------------------------------------

    def atualiza_amostra(self, t, valores):
        pid = self._pid
        if pid is None:
            return
        self._t_amostra = t
        h = self.app.contas_para_altura_ativa(valores['LT'])

        if h > MALHA_H_DESARME_MM:
            self._desativa(f'desarme de seguranca: h = {h:.0f} mm > '
                           f'{MALHA_H_DESARME_MM:.0f} mm', desliga_bomba=True)
            messagebox.showwarning(
                'Desarme de seguranca',
                f'O PID foi desativado e PUMP2 foi desligada: o nivel passou de '
                f'{MALHA_H_DESARME_MM:.0f} mm.')
            return

        # Relê os campos a cada amostra: entrada invalida (ou pela metade, ex.:
        # campo vazio durante a digitacao) mantem os valores anteriores.
        p, aviso = self._le_a_quente()
        self._p = p
        if dict(p, aw=self.var_aw.get()) != self.registro[-1]['p']:
            self._fecha_segmento(t)             # mudanca a quente: novo segmento
            self._abre_segmento(t, p)
        self._aplica_parametros(p)
        sp = p['sp']

        if self._t_ult is not None and t - self._t_ult < pid.T - 1e-6:
            return
        if self._t_ult is None:
            pid.h_ant = h              # 1o calculo: derivada nula
        self._t_ult = t

        uc, u, P, I, D = pid.calcula(sp, h)
        self.app.aplica_comando(self.app.valve_pct, uc)   # VALVE e do usuario

        e = sp - h
        self.app.gr.acrescenta_referencia(t, sp)
        self.app.gr.acrescenta(t, {'u': u, 'uc': uc, 'e': e, 'uK': P, 'uTi': I, 'uD': D})

        texto = (f'h = {h:.1f} mm | r = {sp:.0f} | e = {e:+.1f} | u_c = {uc:.1f} % '
                 f'(P {P:+.1f} | I {I:+.1f} | D {D:+.1f}) | u = {u:+.1f} %')
        self.lb.configure(
            text='ativo.' + aviso if aviso else 'ativo. Ganhos e set-point valem a quente.',
            foreground='#a11')
        self.app.status_ensaio(texto + aviso)


# ---------------------------------------------------------------------------
# Janela principal
# ---------------------------------------------------------------------------

class Janela(tk.Tk):

    def __init__(self, planta, janela_s=JANELA_S):
        super().__init__()
        self.title('Planta TQ CE117 - hub de ensaios')
        self.geometry('1180x800')
        self.minsize(960, 600)

        # As caixas de dialogo padrao (messagebox.show*/askyesno) usam por
        # padrao um wrapLength estreito (poucas polegadas), o que deixa o
        # texto alto e cheio de quebras de linha em avisos mais longos.
        # "*Dialog.msg.wrapLength" e o padrao documentado do Tk para alargar
        # essas caixas (nao tem efeito nos alertas nativos do macOS, que ja
        # calculam sua propria largura).
        self.option_add('*Dialog.msg.wrapLength', '6i')

        # Em alguns temas ttk do Linux (ex.: Ubuntu com tema 'default'/'clam'
        # herdado do GTK) a altura de linha padrao do Treeview e calculada
        # curta demais para a fonte do sistema, cortando os numeros das
        # tabelas (calibracao de LT, varredura, escada) ao meio. Calcula a
        # altura a partir da metrica real da fonte em vez de confiar no
        # padrao do tema.
        fonte_tabela = tkfont.nametofont('TkDefaultFont')
        ttk.Style(self).configure(
            'Treeview', rowheight=fonte_tabela.metrics('linespace') + 6)

        self.fila = queue.Queue()
        self.aquisicao = Aquisicao(planta, self.fila)

        # comando manual (sliders) - refletem o ultimo valor aplicado, seja
        # pelos proprios sliders, seja por um ensaio automatico
        self.valve_pct = 0.0
        self.pump2_pct = 0.0
        self.controle_owner = None    # None = sliders; string = nome do ensaio dono
        # True so durante a malha da Aula 4: o slider de VALVE fica liberado para
        # o aluno injetar disturbio de vazao e a malha respeita o valor dele; o
        # intertravamento (VALVE >= VALVE_MIN_COM_BOMBA com PUMP2 ligada) continua valendo.
        self.valve_livre = False
        self.zerar_ao_sair = tk.BooleanVar(value=True)

        self.janela_s = float(janela_s)
        self.janelas = dict(JANELAS)
        self.janelas.setdefault(rotulo_janela(self.janela_s), self.janela_s)
        self.janela_txt = tk.StringVar(value=rotulo_janela(self.janela_s))

        # Historico bruto de amostras (t, valores em contas), mantido pela
        # mesma janela maxima do grafico - usado pelo botao "exportar dados"
        # para gravar o CSV/PDF da janela de tempo selecionada no grafico.
        self._historico = deque()

        self._abas = []
        self._monta()
        self.protocol('WM_DELETE_WINDOW', self.encerra)

        self.aquisicao.start()
        self.after(100, self._drena_fila)

    # -- construcao da tela --------------------------------------------

    def _monta(self):
        # Barra superior dividida em duas: comando manual (esquerda) e PID livre
        # (direita).
        barra_comando = ttk.Frame(self)
        barra_comando.pack(fill='x', padx=10, pady=(10, 6))
        comando = ttk.LabelFrame(barra_comando, text='Comando manual', padding=10)
        comando.pack(side='left', fill='both', expand=True, padx=(0, 6))

        ttk.Label(comando, text='VALVE (S):').grid(row=0, column=0, sticky='w')
        self.var_slider_valve = tk.DoubleVar(value=0.0)
        self.sl_valve = ttk.Scale(
            comando, from_=0, to=100, orient='horizontal',
            variable=self.var_slider_valve, command=self._slider_valve_moveu, length=140)
        self.sl_valve.grid(row=0, column=1, sticky='we', padx=8)
        self.sl_valve.bind('<Left>', lambda _e: self._incrementa_valve(-PASSO_SLIDER) or 'break')
        self.sl_valve.bind('<Down>', lambda _e: self._incrementa_valve(-PASSO_SLIDER) or 'break')
        self.sl_valve.bind('<Right>', lambda _e: self._incrementa_valve(PASSO_SLIDER) or 'break')
        self.sl_valve.bind('<Up>', lambda _e: self._incrementa_valve(PASSO_SLIDER) or 'break')
        self.lb_slider_valve = ttk.Label(comando, text='0.0 %', width=7)
        self.lb_slider_valve.grid(row=0, column=2, sticky='w')
        self._monta_setas(comando, row=0, column=3,
                           on_menos=lambda: self._incrementa_valve(-PASSO_SLIDER),
                           on_mais=lambda: self._incrementa_valve(PASSO_SLIDER))
        self.bt_valve = tk.Button(comando, width=14, command=self._alterna_valve_botao)
        self.bt_valve.grid(row=0, column=4, sticky='w', padx=(14, 0))

        ttk.Label(comando, text='PUMP2:').grid(row=1, column=0, sticky='w', pady=(6, 0))
        self.var_slider_pump2 = tk.DoubleVar(value=0.0)
        self.sl_pump2 = ttk.Scale(
            comando, from_=0, to=100, orient='horizontal',
            variable=self.var_slider_pump2, command=self._slider_pump2_moveu, length=140)
        self.sl_pump2.grid(row=1, column=1, sticky='we', padx=8, pady=(6, 0))
        self.sl_pump2.bind('<Left>', lambda _e: self._incrementa_pump2(-PASSO_SLIDER) or 'break')
        self.sl_pump2.bind('<Down>', lambda _e: self._incrementa_pump2(-PASSO_SLIDER) or 'break')
        self.sl_pump2.bind('<Right>', lambda _e: self._incrementa_pump2(PASSO_SLIDER) or 'break')
        self.sl_pump2.bind('<Up>', lambda _e: self._incrementa_pump2(PASSO_SLIDER) or 'break')
        self.lb_slider_pump2 = ttk.Label(comando, text='0.0 %', width=7)
        self.lb_slider_pump2.grid(row=1, column=2, sticky='w', pady=(6, 0))
        self._monta_setas(comando, row=1, column=3,
                           on_menos=lambda: self._incrementa_pump2(-PASSO_SLIDER),
                           on_mais=lambda: self._incrementa_pump2(PASSO_SLIDER))
        self.bt_pump2 = tk.Button(comando, width=14, command=self._alterna_pump2_botao)
        self.bt_pump2.grid(row=1, column=4, sticky='w', padx=(14, 0), pady=(6, 0))

        ttk.Checkbutton(comando, text='zerar saidas ao sair',
                        variable=self.zerar_ao_sair).grid(
            row=2, column=0, columnspan=5, sticky='w', pady=(6, 0))
        comando.columnconfigure(1, weight=1)

        self.painel_pid = PainelPID(barra_comando, self, padding=10)
        self.painel_pid.pack(side='left', fill='y')
        self._abas.append(self.painel_pid)

        painel = ttk.Panedwindow(self, orient='vertical')
        painel.pack(fill='both', expand=True, padx=10, pady=(0, 6))

        # Leitura de todas as tags (sempre visivel) e grafico, lado a lado,
        # com uma divisoria arrastavel com o mouse entre os dois.
        painel_topo = ttk.Panedwindow(painel, orient='horizontal')

        self.painel_leituras = PainelLeituras(
            painel_topo, on_calibracao=self._define_calibracao_lt, padding=10)
        painel_topo.add(self.painel_leituras, weight=1)
        self._abas.append(self.painel_leituras)

        quadro_grafico = ttk.Frame(painel_topo)
        janela = ttk.Frame(quadro_grafico)
        janela.pack(fill='x')
        ttk.Label(janela, text='janela do grafico:').pack(side='left')
        self.cb_janela = ttk.Combobox(
            janela, textvariable=self.janela_txt, width=8, state='readonly',
            values=sorted(self.janelas, key=self.janelas.get))
        self.cb_janela.pack(side='left', padx=(4, 10))
        self.cb_janela.bind('<<ComboboxSelected>>', self._troca_janela)
        self.bt_modo_grafico = ttk.Button(
            janela, text='ver como linha', command=self._alterna_modo_grafico)
        self.bt_modo_grafico.pack(side='left', padx=(10, 6))
        self.bt_escala_grafico = ttk.Button(
            janela, text='escala adaptativa', command=self._alterna_escala_grafico)
        self.bt_escala_grafico.pack(side='left', padx=(0, 6))

        ttk.Button(janela, text='limpar grafico', command=lambda: self.gr.limpa()).pack(side='left')

        self.bt_pausar = ttk.Button(
            janela, text='pausar visualizacao', command=self._alterna_pausa)
        self.bt_pausar.pack(side='left', padx=(14, 0))

        self.bt_exportar = ttk.Button(
            janela, text='exportar dados', command=self._alterna_exportacao)
        self.bt_exportar.pack(side='left', padx=(6, 0))
        self.lb_exportar_dica = ttk.Label(
            janela, text='selecione com o mouse a faixa de tempo a ser exportada no grafico abaixo',
            foreground='#555', font=('TkDefaultFont', 8))
        # so aparece durante a selecao (ver _alterna_exportacao/_exporta_janela)

        linha_series = ttk.Frame(quadro_grafico)
        linha_series.pack(fill='x', pady=(4, 0))
        ttk.Label(linha_series, text='mostrar:').pack(side='left')
        self.vars_serie = {}
        for chave, rotulo, cor in SERIES_GRAFICO:
            var = tk.BooleanVar(value=True)
            self.vars_serie[chave] = var
            tk.Checkbutton(
                linha_series, text=rotulo, variable=var, fg=cor, activeforeground=cor,
                selectcolor='white', font=('TkDefaultFont', 9),
                command=lambda c=chave, v=var: self.gr.define_visivel(c, v.get()),
            ).pack(side='left', padx=(6, 0))

        # Curva de altura (h, mm): disponivel desde o inicio, pela calibracao
        # de LT em vigor (a da biblioteca ate que o botao "Ajustar calibracao
        # de LT" do painel de leituras receba a da propria bancada). Qual das
        # duas esta valendo se le no proprio painel de leituras.
        self.var_serie_altura = tk.BooleanVar(value=True)
        self.cb_serie_altura = tk.Checkbutton(
            linha_series, text=ROTULO_ALTURA, variable=self.var_serie_altura,
            fg=COR_ALTURA, activeforeground=COR_ALTURA, selectcolor='white',
            font=('TkDefaultFont', 9),
            command=lambda: self.gr.define_visivel_altura(self.var_serie_altura.get()))
        self.cb_serie_altura.pack(side='left', padx=(6, 0))

        # Curvas do controlador (u, u_c, u_K, u_Ti): o quadro so e empacotado
        # enquanto a malha da Aula 4 esta em curso (`mostra_curvas_controlador`).
        self.quadro_ctrl = ttk.Frame(linha_series)
        self.vars_ctrl = {}
        self.cbs_ctrl = {}
        for chave, rotulo, cor in SERIES_CTRL + (SERIE_ERRO,):
            var = tk.BooleanVar(value=True)
            self.vars_ctrl[chave] = var
            self.cbs_ctrl[chave] = tk.Checkbutton(
                self.quadro_ctrl, text=rotulo, variable=var, fg=cor, activeforeground=cor,
                selectcolor='white', font=('TkDefaultFont', 9),
                command=lambda c=chave, v=var: self.gr.define_visivel(c, v.get()))
            self.cbs_ctrl[chave].pack(side='left', padx=(6, 0))

        # Altura generosa por padrao; o proprio `Panedwindow` deixa o usuario
        # arrastar a divisoria para dar ainda mais (ou menos) espaco ao
        # grafico em relacao as abas logo abaixo.
        self.gr = Grafico(quadro_grafico, janela_s=self.janela_s, height=320)
        self.gr.pack(fill='both', expand=True, pady=(6, 0))

        # Rolagem horizontal pelo historico em memoria (ate JANELA_MAX_S). O
        # arrasto com o botao esquerdo sobre o grafico ja e a selecao de
        # exportacao, por isso o deslocamento fica numa barra propria em vez
        # de ser feito arrastando a tela.
        rolagem = ttk.Frame(quadro_grafico)
        rolagem.pack(fill='x', pady=(2, 0))
        self.barra_tempo = ttk.Scrollbar(
            rolagem, orient='horizontal', command=self._rola_grafico)
        self.barra_tempo.pack(side='left', fill='x', expand=True)
        self.bt_ao_vivo = ttk.Button(
            rolagem, text='ao vivo', width=9, command=self._volta_ao_vivo)
        self.bt_ao_vivo.pack(side='left', padx=(6, 0))
        self.gr.on_rolagem = self._atualiza_barra_tempo
        painel_topo.add(quadro_grafico, weight=3)

        painel.add(painel_topo, weight=3)

        self.notebook = ttk.Notebook(painel)
        painel.add(self.notebook, weight=2)

        self._adiciona_aba('Aula 1', AbaAula1)
        self._adiciona_aba('Aula 2', AbaAula2)
        self._adiciona_aba('Aula 3', AbaAula3)
        self._adiciona_aba('Aula 4', AbaAula4)
        # O painel do PID livre so aparece com a aba da Aula 4 ativa.
        self.notebook.bind('<<NotebookTabChanged>>', self._atualiza_visib_pid)
        self._atualiza_visib_pid()

        self.lb_status = ttk.Label(self, text='iniciando...', anchor='w',
                                   relief='sunken', padding=(6, 3))
        self.lb_status.pack(fill='x', padx=10, pady=(0, 10))

        self._atualiza_controles()

    def _atualiza_visib_pid(self, _evento=None):
        """Mostra o painel do PID livre so quando a aba 'Aula 4' esta ativa.
        Esconder o painel nao desativa um PID em curso: ele segue rodando."""
        ativa = self.notebook.tab(self.notebook.select(), 'text') == 'Aula 4'
        if ativa and not self.painel_pid.winfo_manager():
            self.painel_pid.pack(side='left', fill='y')
        elif not ativa and self.painel_pid.winfo_manager():
            self.painel_pid.pack_forget()

    def _monta_setas(self, mestre, row, column, on_menos, on_mais):
        """Par de botoes '<'/'>' para incrementar/decrementar um slider em
        passos de `PASSO_SLIDER`, ao lado do rotulo de percentual."""
        quadro = ttk.Frame(mestre)
        quadro.grid(row=row, column=column, sticky='w', padx=(4, 0), pady=(6, 0) if row else 0)
        ttk.Button(quadro, text='◀', width=2, command=on_menos).pack(side='left')
        ttk.Button(quadro, text='▶', width=2, command=on_mais).pack(side='left', padx=(2, 0))

    def _adiciona_aba(self, texto, classe_aba, *args):
        """Cria uma aba dentro de um `QuadroRolavel`, para que abas com muitos
        controles rolem em vez de espremer (ou cortar) o resto da janela."""
        quadro = QuadroRolavel(self.notebook)
        self.notebook.add(quadro, text=texto)
        aba = classe_aba(quadro.interior, self, *args)
        aba.pack(fill='both', expand=True)
        self._abas.append(aba)
        return aba

    # -- intertravamento e controle manual x automatico ------------------

    def pede_controle(self, nome):
        """Um ensaio pede o controle exclusivo de VALVE/PUMP2.

        Devolve False (e avisa) se ja houver outro ensaio em curso.
        """
        if self.controle_owner is not None:
            messagebox.showwarning(
                'Controle ocupado',
                f'"{self.controle_owner}" ja esta atuando na planta. '
                'Pare esse ensaio antes de iniciar outro.')
            return False
        self.controle_owner = nome
        self._atualiza_controles()
        return True

    def libera_controle(self):
        self.controle_owner = None
        self._atualiza_controles()

    def status_ensaio(self, texto):
        """Progresso de um ensaio em curso na barra de status do rodape.

        Enquanto um ensaio detem o controle, a barra fica parada em
        "controle automatico: <nome>" (`_atualiza_controles`), porque a
        atualizacao por amostra do rodape so roda com `controle_owner` nulo.
        Num ensaio longo - a varredura da Aula 3 tem 21 patamares - isso
        deixa a barra sem informacao nenhuma justamente quando ela e mais
        util. Um ensaio chama este metodo a cada amostra para dizer, ali
        mesmo, em que passo esta.

        Ignora chamadas de quem nao detem o controle (um ensaio que acabou de
        ser encerrado, por exemplo): a barra volta sozinha as leituras na
        proxima amostra.
        """
        if self.controle_owner is None:
            return
        self.lb_status.configure(text=f'{self.controle_owner}: {texto}', foreground='#a11')

    def _valve_permite_pump2(self):
        return self.valve_pct >= VALVE_MIN_COM_BOMBA - 1e-6

    def _valve_minimo(self):
        """Menor VALVE aceita agora: com PUMP2 ligada, VALVE_MIN_COM_BOMBA."""
        return VALVE_MIN_COM_BOMBA if self.pump2_pct > 1e-6 else 0.0

    def _atualiza_controles(self):
        automatico = self.controle_owner is not None
        estado = 'disabled' if automatico and not self.valve_livre else 'normal'
        self.sl_valve.configure(state=estado)
        # PUMP2 so libera com VALVE >= VALVE_MIN_COM_BOMBA.
        pump2_liberado = not automatico and self._valve_permite_pump2()
        self.sl_pump2.configure(state='normal' if pump2_liberado else 'disabled')

        self._pinta_botao(self.bt_valve, 'VALVE (S)', self.valve_pct, habilitado=not automatico)
        self._pinta_botao(self.bt_pump2, 'PUMP2', self.pump2_pct, habilitado=pump2_liberado,
                          motivo_bloqueio=(f'VALVE >= {VALVE_MIN_COM_BOMBA:.0f} % antes'
                                           if not automatico else None))

        if automatico:
            self.lb_status.configure(
                text=f'controle automatico: {self.controle_owner}', foreground='#a11')

    def _pinta_botao(self, botao, nome, pct, habilitado, motivo_bloqueio=None):
        ligado = pct >= 100.0 - 1e-6
        if not habilitado and motivo_bloqueio and not ligado:
            rotulo = f'{nome}\nOFF - {motivo_bloqueio}'
        else:
            rotulo = f'{nome}\n{"ON" if ligado else "OFF"}  ({pct:.1f} %)'
        botao.configure(
            text=rotulo,
            state='normal' if habilitado else 'disabled',
            background='#1e9e4a' if ligado else '#c9ccd1',
            activebackground='#26b356' if ligado else '#d8dbe0',
            disabledforeground='white' if ligado else '#666',
            foreground='white' if ligado else 'black',
        )

    def aplica_comando(self, valve_pct, pump2_pct):
        """Ponto UNICO de escrita de VALVE/PUMP2: sliders, botoes e ensaios passam por aqui.

        Intertravamento: PUMP2 so fica ligada com VALVE >= VALVE_MIN_COM_BOMBA;
        senao PUMP2 e zerada aqui (rede de seguranca - os sliders e botoes
        recusam o pedido antes de chegar aqui).
        """
        valve_pct = max(0.0, min(100.0, valve_pct))
        pump2_pct = max(0.0, min(100.0, pump2_pct))
        if valve_pct < VALVE_MIN_COM_BOMBA - 1e-6:
            pump2_pct = 0.0
        self.valve_pct, self.pump2_pct = valve_pct, pump2_pct

        self.aquisicao.setpoint(
            percentual_para_conta(valve_pct), percentual_para_conta(pump2_pct))

        # reflete nos sliders sem disparar de volta o callback de comando
        self.var_slider_valve.set(valve_pct)
        self.var_slider_pump2.set(pump2_pct)
        self.lb_slider_valve.configure(text=f'{valve_pct:.1f} %')
        self.lb_slider_pump2.configure(text=f'{pump2_pct:.1f} %')
        self._atualiza_controles()

    def _avisa_pump2_bloqueado(self):
        messagebox.showwarning(
            'Intertravamento',
            f'PUMP2 bloqueado: abra a valvula S em pelo menos {VALVE_MIN_COM_BOMBA:.0f} % '
            'antes de acionar a bomba (a bomba contra a valvula quase fechada '
            'pressuriza a linha).')

    def _avisa_valve_bloqueada(self):
        messagebox.showwarning(
            'Intertravamento',
            f'VALVE nao pode ficar abaixo de {VALVE_MIN_COM_BOMBA:.0f} % com PUMP2 '
            'ligada. Desligue PUMP2 antes de fechar mais a valvula.')

    def define_valve_livre(self, livre):
        """Liga/desliga o slider de VALVE durante a malha da Aula 4 (o
        intertravamento continua valendo); chamado por `AbaAula4`."""
        self.valve_livre = livre
        self._atualiza_controles()

    def _slider_valve_moveu(self, _valor):
        if self.controle_owner is not None and not self.valve_livre:
            return
        novo = _arredonda_passo(self.var_slider_valve.get())
        if novo < self._valve_minimo() - 1e-6:
            novo = self._valve_minimo()
            self.var_slider_valve.set(novo)
        self.aplica_comando(novo, self.pump2_pct)

    def _slider_pump2_moveu(self, _valor):
        if self.controle_owner is not None:
            return
        if not self._valve_permite_pump2():
            self.var_slider_pump2.set(0.0)
            self._avisa_pump2_bloqueado()
            return
        self.aplica_comando(self.valve_pct, _arredonda_passo(self.var_slider_pump2.get()))

    def _incrementa_valve(self, delta):
        if self.controle_owner is not None and not self.valve_livre:
            return
        novo = max(self._valve_minimo(), _arredonda_passo(self.valve_pct + delta))
        self.aplica_comando(novo, self.pump2_pct)

    def _incrementa_pump2(self, delta):
        if self.controle_owner is not None:
            return
        if not self._valve_permite_pump2():
            self._avisa_pump2_bloqueado()
            return
        novo = _arredonda_passo(self.pump2_pct + delta)
        self.aplica_comando(self.valve_pct, novo)

    def _alterna_valve_botao(self):
        if self.controle_owner is not None:
            return
        ligada = self.valve_pct >= 100.0 - 1e-6
        if ligada and self._valve_minimo() > 0.0:
            self._avisa_valve_bloqueada()
            return
        novo = 0.0 if ligada else 100.0
        self.aplica_comando(novo, self.pump2_pct)

    def _alterna_pump2_botao(self):
        if self.controle_owner is not None:
            return
        if not self._valve_permite_pump2():
            self._avisa_pump2_bloqueado()
            return
        novo = 0.0 if self.pump2_pct >= 100.0 - 1e-6 else 100.0
        self.aplica_comando(self.valve_pct, novo)

    # -- janela do grafico ------------------------------------------------

    def _troca_janela(self, _evento=None):
        segundos = self.janelas.get(self.janela_txt.get())
        if segundos is None:
            return
        self.janela_s = segundos
        self.gr.define_janela(segundos)
        self.cb_janela.selection_clear()

    # -- calibracao de LT (definida pelo botao do painel de leituras) ------

    # Visualizacao padrao ao iniciar a malha da Aula 4 (o usuario pode
    # alterar pelos checkboxes durante o ensaio).
    VISUALIZACAO_MALHA = {
        'LT': False, 'FT2': False, 'PUMP2': True, 'VALVE': False,
        'u': False, 'uc': True, 'uK': False, 'uTi': False, 'uD': False, 'e': True,
    }

    def _aplica_visualizacao_malha(self):
        for chave, visivel in self.VISUALIZACAO_MALHA.items():
            var = self.vars_serie.get(chave) or self.vars_ctrl.get(chave)
            if var is not None:
                var.set(visivel)
            self.gr.visiveis[chave] = visivel
        self.var_serie_altura.set(True)
        self.gr.define_visivel_altura(True)

    def mostra_curvas_controlador(self, ativo, com_derivativo=False):
        """Liga/desliga as curvas u, u_c, u_K, u_Ti (e u_D, se `com_derivativo`) e
        seus checkboxes; chamado por `AbaAula4` e por `PainelPID` ao iniciar e
        ao encerrar a malha."""
        if ativo:
            cb_d, cb_e = self.cbs_ctrl['uD'], self.cbs_ctrl['e']
            if com_derivativo:
                cb_d.pack(side='left', padx=(6, 0), before=cb_e)
            else:
                cb_d.pack_forget()
            self._aplica_visualizacao_malha()
            self.quadro_ctrl.pack(side='left', padx=(10, 0))
        else:
            self.quadro_ctrl.pack_forget()
        self.gr.define_ctrl_ativo(ativo)

    def _define_calibracao_lt(self, grau, coefs):
        """Callback do `PainelLeituras`: `grau`/`coefs` sao None quando o
        usuario volta a calibracao da biblioteca (`conversoes.py`).

        A troca vale imediatamente para tudo que o hub expressa em mm - a
        tabela de leituras, a curva de altura do grafico, o CSV/PDF da
        exportacao e os CSV das abas das Aulas 2 e 3 -, porque todos passam
        por `contas_para_altura_ativa`. Ensaios ja gravados nao sao
        reescritos: a calibracao vale a partir daqui.
        """
        self.gr.define_visivel_altura(self.var_serie_altura.get())
        self.gr.redesenha()
        rotulo = self.rotulo_calibracao_lt()
        if self.controle_owner is None:
            self.lb_status.configure(
                text=f'calibracao de LT: {rotulo}. Vale para o grafico, para a '
                     'exportacao e para os ensaios das Aulas 2 e 3.',
                foreground='#0a7d32')

    def contas_para_altura_ativa(self, conta):
        return self.painel_leituras.contas_para_altura_ativa(conta)

    def rotulo_calibracao_lt(self):
        return self.painel_leituras.rotulo_calibracao()

    def confirma_calibracao_lt(self, nome_ensaio):
        """Confirma com o usuario, antes de um ensaio das Aulas 2/3, que a
        calibracao de LT em vigor e mesmo a que ele quer gravar.

        O erro que isto evita e silencioso e caro: gravar um ensaio inteiro
        com os coeficientes piloto da biblioteca em vez dos levantados na
        propria bancada na Aula 1 - o CSV sai plausivel, so que numa escala
        de nivel que nao e a desta planta. Devolve False se o usuario decidir
        colar a calibracao antes de comecar.
        """
        if self.painel_leituras.calibracao_da_sessao() is not None:
            return True
        return messagebox.askyesno(
            'Calibracao de LT',
            f'O {nome_ensaio} vai gravar a coluna h_mm com os coeficientes da '
            'BIBLIOTECA (comum/conversoes.py), levantados numa bancada piloto - '
            'nenhuma calibracao foi colada nesta sessao.\n\n'
            'Se voce ja tem os coeficientes da SUA bancada (Aula 1), cancele e '
            'cole-os em "Ajustar calibracao de LT", no painel de leituras.\n\n'
            'Gravar assim mesmo?')

    # -- pausa e exportacao do grafico -------------------------------------

    def _alterna_pausa(self):
        if self.gr.pausado:
            self.gr.retoma()
            self.bt_pausar.configure(text='pausar visualizacao')
        else:
            self.gr.pausa()
            self.bt_pausar.configure(text='retomar visualizacao')

    def _alterna_modo_grafico(self):
        self.gr.alterna_modo()
        if self.gr.modo == 'dispersao':
            self.bt_modo_grafico.configure(text='ver como linha')
        else:
            self.bt_modo_grafico.configure(text='ver como dispersao')

    def _rola_grafico(self, *args):
        """Comando da barra de rolagem horizontal do grafico.

        Recebe os tres formatos que `ttk.Scrollbar` emite: arrasto da alca
        ('moveto', fracao), setas das pontas ('scroll', n, 'units') e clique
        no vao ('scroll', n, 'pages').
        """
        if args[0] == 'moveto':
            self.gr.rola_para(float(args[1]))
        elif args[0] == 'scroll':
            passo = 0.1 if args[2] == 'units' else 0.9
            self.gr.rola_passos(int(args[1]), passo)

    def _atualiza_barra_tempo(self, inicio, fim):
        """Chamado pelo grafico a cada redesenho, com a fatia visivel do
        historico. Tambem e aqui que o botao "ao vivo" acende ou apaga."""
        self.barra_tempo.set(inicio, fim)
        self.bt_ao_vivo.state(['disabled'] if self.gr.ao_vivo() else ['!disabled'])

    def _volta_ao_vivo(self):
        self.gr.volta_ao_vivo()

    def _alterna_escala_grafico(self):
        """Chaveia entre as faixas fixas (0-100 % e 0-250 mm) e faixas ajustadas
        ao minimo e ao maximo do que esta em tela. O rotulo do botao nomeia
        sempre o modo de DESTINO, como o de "ver como linha"."""
        self.gr.alterna_escala()
        self.bt_escala_grafico.configure(
            text='escala fixa' if self.gr.escala_adaptativa else 'escala adaptativa')

    def _alterna_exportacao(self):
        if self.gr.selecionando:
            self.gr.desativa_selecao()
            self.bt_exportar.configure(text='exportar dados')
            self.lb_exportar_dica.pack_forget()
            self.lb_status.configure(text='selecao cancelada.', foreground='#333')
            return

        if not self._historico:
            messagebox.showwarning('Sem dados', 'Ainda nao ha amostras para exportar.')
            return

        # A visualizacao e pausada automaticamente: arrastar a selecao com o
        # grafico ainda rolando ao vivo deslocaria o eixo do tempo debaixo do
        # mouse durante o arrasto.
        if not self.gr.pausado:
            self._alterna_pausa()

        self.gr.ativa_selecao(self._exporta_janela)
        self.bt_exportar.configure(text='cancelar selecao')
        self.lb_exportar_dica.pack(side='left', padx=(8, 0))
        self.lb_status.configure(
            text='arraste o mouse sobre o grafico para selecionar a janela a exportar.',
            foreground='#333')

    def _exporta_janela(self, t_ini, t_fim):
        self.bt_exportar.configure(text='exportar dados')
        self.lb_exportar_dica.pack_forget()
        t_ini, t_fim = min(t_ini, t_fim), max(t_ini, t_fim)
        linhas = [(t, valores) for t, valores in self._historico if t_ini - 1e-6 <= t <= t_fim + 1e-6]
        if not linhas:
            messagebox.showwarning('Sem dados', 'Nao ha amostras na janela selecionada.')
            return

        caminho_csv = filedialog.asksaveasfilename(
            defaultextension='.csv', initialfile='exportacao_grafico.csv',
            filetypes=[('CSV', '*.csv')])
        if not caminho_csv:
            return

        self._salva_csv_exportacao(caminho_csv, linhas)
        caminho_pdf = os.path.splitext(caminho_csv)[0] + '.pdf'
        ok_pdf, erro_pdf = self._salva_pdf_exportacao(caminho_pdf, linhas)

        resumo = f'{len(linhas)} amostras exportadas.\n\nCSV: {caminho_csv}'
        if any(self.painel_pid.parametros_em(t) is not None for t, _v in linhas):
            resumo += '\n(com as colunas pid_* dos parametros do PID em vigor)'
        if ok_pdf:
            resumo += f'\nPDF: {caminho_pdf}'
        else:
            resumo += f'\n\nPDF nao gerado ({erro_pdf}).'
        messagebox.showinfo('Exportacao concluida', resumo)
        self.lb_status.configure(
            text=f'exportado: {len(linhas)} amostras.', foreground='#0a7d32')

    def _salva_csv_exportacao(self, caminho, linhas):
        t0 = linhas[0][0]
        # Se havia PID livre ativo em alguma amostra do recorte, o CSV ganha as
        # colunas com os parametros que estavam em vigor em cada amostra.
        com_pid = any(self.painel_pid.parametros_em(t) is not None for t, _v in linhas)
        with open(caminho, 'w', newline='') as arquivo:
            escritor = csv.writer(arquivo)
            escritor.writerow(COLUNAS_EXPORTACAO + (COLUNAS_PID_EXPORTACAO if com_pid else []))
            for t, valores in linhas:
                lt, ft2 = valores['LT'], valores['FT2']
                pt, tt5 = valores['PT'], valores['TT5']
                pump2, valve = valores['PUMP2'], valores['VALVE']
                escritor.writerow([
                    f'{t - t0:.3f}',
                    lt, f'{conta_para_volts(lt):.4f}', f'{self.contas_para_altura_ativa(lt):.3f}',
                    ft2, f'{conta_para_volts(ft2):.4f}', f'{contas_para_vazao(ft2):.4f}',
                    pt, f'{conta_para_volts(pt):.4f}',
                    tt5, f'{conta_para_volts(tt5):.4f}',
                    pump2, f'{conta_para_volts(pump2):.4f}', f'{conta_para_percentual(pump2):.1f}',
                    valve, f'{conta_para_volts(valve):.4f}', f'{conta_para_percentual(valve):.1f}',
                ] + (self.painel_pid.linha_exportacao(t) if com_pid else []))

    def _salva_pdf_exportacao(self, caminho, linhas):
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
        except ImportError as erro:
            return False, str(erro)

        t0 = linhas[0][0]
        ts = [t - t0 for t, _v in linhas]
        fig, eixo = plt.subplots(figsize=(8, 4.5))
        # So entram as curvas que estavam com o checkbox de visualizacao
        # marcado no grafico ao vivo - o CSV continua com todas as tags. O
        # modo de visualizacao (linha cheia interpolando vs. dispersao com
        # segurador de ordem zero) tambem segue o que esta selecionado no
        # grafico ao vivo no momento da exportacao.
        modo_dispersao = self.gr.modo == 'dispersao'
        n_visiveis = 0
        for chave, rotulo, cor in SERIES_GRAFICO:
            if not self.gr.visiveis.get(chave, True):
                continue
            pcts = [conta_para_percentual(valores[chave]) for _t, valores in linhas]
            if modo_dispersao:
                eixo.step(ts, pcts, where='post', color=cor, label=rotulo,
                         linewidth=0.8, linestyle='--', alpha=0.6)
                eixo.scatter(ts, pcts, color=cor, s=22, zorder=3)
            else:
                eixo.plot(ts, pcts, color=cor, label=rotulo, linewidth=1.5)
            n_visiveis += 1
        eixo.set_xlabel('t [s]')
        eixo.set_ylabel('% do fundo de escala do instrumento')
        # o PDF sai no mesmo modo de escala do grafico ao vivo; no adaptativo,
        # quem enquadra e o proprio matplotlib, sobre os dados do recorte
        if not self.gr.escala_adaptativa:
            eixo.set_ylim(-5, 105)
        eixo.grid(True, color='#e8e8e8')
        eixo.set_title('Planta TQ CE117 - janela exportada do grafico')

        linhas_legenda, rotulos_legenda = [], []
        eixo_leg, rotulos_leg = eixo.get_legend_handles_labels()
        linhas_legenda += eixo_leg
        rotulos_legenda += rotulos_leg

        # Curva de altura (h, mm): so entra se estava disponivel e com o
        # checkbox marcado no grafico ao vivo, no eixo secundario proprio -
        # mesma logica de `Grafico.redesenha` (ver comentario de
        # ROTULO_ALTURA/COR_ALTURA).
        if self.gr.altura_disponivel and self.gr.altura_visivel:
            h_mm = [self.contas_para_altura_ativa(valores['LT']) for _t, valores in linhas]
            eixo_alt = eixo.twinx()
            if modo_dispersao:
                eixo_alt.step(ts, h_mm, where='post', color=COR_ALTURA, label=ROTULO_ALTURA,
                             linewidth=0.8, linestyle='--', alpha=0.6)
                eixo_alt.scatter(ts, h_mm, color=COR_ALTURA, s=22, zorder=3)
            else:
                eixo_alt.plot(ts, h_mm, color=COR_ALTURA, label=ROTULO_ALTURA, linewidth=1.5)
            eixo_alt.set_ylabel('h [mm]', color=COR_ALTURA)
            # mesma faixa fixa do grafico ao vivo: o PDF exportado e a figura
            # que vai para o relatorio, e ele tem de sair na escala em que o
            # aluno viu o ensaio acontecer.
            if not self.gr.escala_adaptativa:
                eixo_alt.set_ylim(*faixa_altura(-5.0, 105.0))
            eixo_alt.tick_params(axis='y', labelcolor=COR_ALTURA)
            alt_leg, alt_rot = eixo_alt.get_legend_handles_labels()
            linhas_legenda += alt_leg
            rotulos_legenda += alt_rot
            n_visiveis += 1

        # Legenda fora da area das curvas (abaixo do eixo, em linha), para
        # nunca sobrepor o grafico - ao contrario de loc='best'/'upper
        # right', que pode cair em cima de uma curva dependendo dos dados.
        if n_visiveis:
            eixo.legend(linhas_legenda, rotulos_legenda, loc='upper center',
                       bbox_to_anchor=(0.5, -0.14), ncol=min(n_visiveis, 4),
                       fontsize=8, frameon=False)
        fig.savefig(caminho, bbox_inches='tight')
        plt.close(fig)
        return True, None

    def _apara_historico(self):
        if not self._historico:
            return
        t_fim = self._historico[-1][0]
        while self._historico and t_fim - self._historico[0][0] > JANELA_MAX_S:
            self._historico.popleft()

    # -- atualizacao --------------------------------------------------------

    def _drena_fila(self):
        try:
            while True:
                evento = self.fila.get_nowait()
                if evento[0] == 'amostra':
                    _, t, valores = evento
                    valores_pct = {chave: conta_para_percentual(valores[chave])
                                   for chave, _r, _c in SERIES_GRAFICO}
                    self.gr.acrescenta(t, valores_pct)
                    self.gr.acrescenta_altura(t, self.contas_para_altura_ativa(valores['LT']))
                    self.gr.redesenha()
                    self._historico.append((t, valores))
                    self._apara_historico()
                    for aba in self._abas:
                        aba.atualiza_amostra(t, valores)
                    if self.controle_owner is None:
                        self.lb_status.configure(
                            text=f'LT {self.contas_para_altura_ativa(valores["LT"]):6.1f} mm   |   '
                                 f'FT2 {contas_para_vazao(valores["FT2"]):5.2f} L/min   |   '
                                 f'VALVE {conta_para_percentual(valores["VALVE"]):5.1f} %   |   '
                                 f'PUMP2 {conta_para_percentual(valores["PUMP2"]):5.1f} %',
                            foreground='#333')
                else:
                    _, texto, ok = evento
                    if self.controle_owner is None:
                        self.lb_status.configure(
                            text=texto, foreground='#0a7d32' if ok else '#a11')
        except queue.Empty:
            pass
        self.after(100, self._drena_fila)

    def encerra(self):
        self.aquisicao.zerar_ao_sair = self.zerar_ao_sair.get()
        self.aquisicao.parar.set()
        self.lb_status.configure(text='encerrando...', foreground='#333')
        self.update_idletasks()
        self.aquisicao.join(timeout=5.0)
        self.destroy()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--ip', default=PLC_IP, help=f'IP do CLP (padrao: {PLC_IP})')
    ap.add_argument('--sim', action='store_true',
                    help='usa um tanque simulado, sem tocar no CLP')
    ap.add_argument('--janela', type=float, default=JANELA_S, metavar='S',
                    help=f'janela inicial do grafico, em segundos (padrao: {JANELA_S:.0f})')
    args = ap.parse_args()

    planta = PlantaSimulada() if args.sim else PlantaCLP(args.ip)
    try:
        Janela(planta, janela_s=args.janela).mainloop()
    except tk.TclError as erro:
        sys.exit(f'ERRO ao abrir a janela ({erro}). Ha display grafico disponivel?')


if __name__ == '__main__':
    main()
