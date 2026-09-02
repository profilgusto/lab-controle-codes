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
    seguranca da Aula 1: PUMP2 so e liberado com VALVE em 100 % (nao basta
    S estar parcialmente aberta);
  - um `ttk.Notebook` com uma aba por aula; cada aba tem as ferramentas de
    ensaio daquela aula (por enquanto, Aulas 1 a 3 - as demais aparecem como
    "em desenvolvimento", no mesmo estado do roteiro).

Toda aba que precisa gravar um CSV (calibracao de LT, esvaziamento, degrau) o
faz a partir do MESMO fluxo de amostras da thread de aquisicao - nao abre uma
segunda conexao com o CLP.

Uso (a partir da pasta ensaios-gui/):
    python3 -W ignore hub_planta.py
    python3 -W ignore hub_planta.py --sim   (sem CLP, tanque simulado)
    python3 -W ignore hub_planta.py --ip 200.200.200.25

Requisito: tkinter (sistema, nao vem do pip) e numpy (so para a calibracao de
LT, na Aba 1). Ver `requirements.txt`.
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
           ('10 min', 600.0), ('30 min', 1800.0))
JANELA_MAX_S = max(s for _r, s in JANELAS)

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
SERIES_GRAFICO = (
    ('LT', 'LT (nivel)', '#0a6ebd'),
    ('FT2', 'FT2 (vazao)', '#d98c00'),
    ('PUMP2', 'PUMP2 (bomba)', '#1e9e4a'),
    ('VALVE', 'VALVE (valvula S)', '#a11'),
)

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
        # A valvula vai sempre na frente da bomba (intertravamento da Aula 1).
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

    Parametros calibrados para reproduzir os numeros do ensaio piloto do
    roteiro (equilibrio ~180 mm, tau ~ 20 s em h0 = 30 mm).
    """

    AREA_MM2 = 1.02e4
    K = 5.47e3
    VAZAO_MAX_MM3_S = 7.34e4

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
        q_out = self.K * math.sqrt(max(0.0, self.h))
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
        self.series = {chave: deque() for chave, _r, _c in SERIES_GRAFICO}
        self.visiveis = {chave: True for chave, _r, _c in SERIES_GRAFICO}
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

        # Modo de visualizacao: 'linha' interpola os pontos amostrados com
        # uma reta cheia (comportamento historico); 'dispersao' marca cada
        # leitura com um ponto grande e visivel, ligado por uma linha fina e
        # semi-transparente em "segurador de ordem zero" (o valor e mantido
        # constante ate a proxima amostra, com subida abrupta no instante da
        # amostra - reflete o que o CLP realmente entrega, sem sugerir uma
        # interpolacao linear entre leituras que nao existe).
        self.modo = 'dispersao'

        # Pausa: "congela" a visualizacao guardando uma copia das series no
        # instante da pausa; `acrescenta()` continua alimentando `self.series`
        # normalmente (a aquisicao nao para), so o desenho passa a ler da
        # copia ate a visualizacao ser retomada.
        self.pausado = False
        self._series_pausadas = None
        self._altura_pausada = None

        # Selecao de uma janela de tempo a arrasto do mouse (usada pelo
        # botao "exportar dados"); `_geom` guarda a ultima geometria de
        # desenho para converter posicao do mouse (px) em instante (t).
        self.selecionando = False
        self._callback_selecao = None
        self._sel_inicio = None
        self._sel_atual = None
        self._geom = None

        self.bind('<Configure>', lambda _e: self.redesenha())
        self.bind('<ButtonPress-1>', self._sel_pressiona)
        self.bind('<B1-Motion>', self._sel_arrasta)
        self.bind('<ButtonRelease-1>', self._sel_solta)

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

    def define_visivel_altura(self, visivel):
        self.altura_visivel = visivel
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

    def acrescenta(self, t, valores_pct):
        for chave, pct in valores_pct.items():
            if chave in self.series:
                self.series[chave].append((t, pct))
        self._descarta_velhos()

    def acrescenta_altura(self, t, h_mm):
        self.serie_altura.append((t, h_mm))
        self._descarta_velhos()

    def limpa(self):
        for pontos in self.series.values():
            pontos.clear()
        self.serie_altura.clear()
        if self._series_pausadas is not None:
            self._series_pausadas = {chave: [] for chave in self.series}
        if self._altura_pausada is not None:
            self._altura_pausada = []
        self.redesenha()

    # -- pausa da visualizacao ------------------------------------------

    def pausa(self):
        self.pausado = True
        self._series_pausadas = {chave: list(pontos) for chave, pontos in self.series.items()}
        self._altura_pausada = list(self.serie_altura)

    def retoma(self):
        self.pausado = False
        self._series_pausadas = None
        self._altura_pausada = None
        self.redesenha()

    def _fonte(self, chave):
        if self.pausado and self._series_pausadas is not None:
            return self._series_pausadas.get(chave, [])
        return self.series[chave]

    def _visiveis(self, chave):
        pontos = self._fonte(chave)
        if not pontos:
            return []
        t_fim = pontos[-1][0]
        return [(t, v) for t, v in pontos if t >= t_fim - self.janela_s]

    def _fonte_altura(self):
        if self.pausado and self._altura_pausada is not None:
            return self._altura_pausada
        return self.serie_altura

    def _visiveis_altura(self):
        pontos = self._fonte_altura()
        if not pontos:
            return []
        t_fim = pontos[-1][0]
        return [(t, v) for t, v in pontos if t >= t_fim - self.janela_s]

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

        todos_pontos = {chave: self._visiveis(chave) for chave, _r, _c in SERIES_GRAFICO}
        t_fim = max((p[-1][0] for p in todos_pontos.values() if p), default=self.janela_s)
        t_ini = max(0.0, t_fim - self.janela_s)
        if t_fim - t_ini < 1.0:
            t_fim = t_ini + 1.0
        v_lo, v_hi = -5.0, 105.0

        def px(t):
            return x0 + (t - t_ini) / (t_fim - t_ini) * (x1 - x0)

        def py(v):
            return y1 - (v - v_lo) / (v_hi - v_lo) * (y1 - y0)

        for pct in (0, 20, 40, 60, 80, 100):
            y = py(pct)
            self.create_line(x0, y, x1, y, fill='#e8e8e8')
            self.create_text(x0 - 6, y, text=f'{pct}%', anchor='e',
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
        for chave, rotulo, cor in SERIES_GRAFICO:
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

        # Curva de altura (h, mm): eixo vertical proprio, a direita, com
        # escala auto-ajustada aos pontos visiveis (nao 0-100 %, como as
        # demais series). So aparece apos uma calibracao de LT ser definida.
        if self.altura_disponivel and self.altura_visivel:
            pontos_alt = self._visiveis_altura()
            if pontos_alt:
                valores_alt = [v for _t, v in pontos_alt]
                alt_min, alt_max = min(valores_alt), max(valores_alt)
            else:
                alt_min, alt_max = 0.0, 250.0
            margem_alt = max(5.0, (alt_max - alt_min) * 0.1)
            hi_alvo = alt_max + margem_alt
            lo_alvo = alt_min - margem_alt

            # O "0" deste eixo (h) e o "0 %" do eixo a esquerda (v_lo=-5,
            # v_hi=105) tem de cair na mesma linha horizontal, para que as
            # duas escalas comparem visualmente a partir da mesma base. Isso
            # significa reservar, abaixo do 0 de h, a MESMA fracao da altura
            # do grafico que o eixo esquerdo reserva abaixo do seu 0 (a
            # faixa de -5 a 0, dentro de -5..105) - dai alt_lo nao ser
            # livre: e sempre -frac0 * (alt_hi - alt_lo).
            frac0 = -v_lo / (v_hi - v_lo)
            escala = max(hi_alvo, 1.0) / (1.0 - frac0)
            if lo_alvo < 0.0:
                escala = max(escala, -lo_alvo / frac0)
            alt_hi = escala * (1.0 - frac0)
            alt_lo = -escala * frac0

            def py_alt(v):
                return y1 - (v - alt_lo) / (alt_hi - alt_lo) * (y1 - y0)

            for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
                v = frac * alt_hi
                self.create_text(x1 + 6, py_alt(v), text=f'{v:.0f}', anchor='w',
                                 font=('TkDefaultFont', 8), fill=COR_ALTURA)
            self.create_text(x1 + 6, y0 - 10, text='h [mm]', anchor='w',
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

            self.create_rectangle(legenda_x, 8, legenda_x + 10, 18, fill=COR_ALTURA, outline='')
            self.create_text(legenda_x + 14, 13, text=ROTULO_ALTURA, anchor='w',
                             font=self._fonte_legenda, fill='#333')
            legenda_x += 14 + self._fonte_legenda.measure(ROTULO_ALTURA) + 20

        self._geom = (x0, y0, x1, y1, t_ini, t_fim)

        if self.selecionando and self._sel_inicio is not None and self._sel_atual is not None:
            xa = max(x0, min(x1, self._sel_inicio))
            xb = max(x0, min(x1, self._sel_atual))
            self.create_rectangle(xa, y0, xb, y1, fill='#3a7bd5', outline='#2a5aa0',
                                  stipple='gray25')


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

    def _remove_ponto(self):
        for item in self.tabela.selection():
            idx = self.tabela.index(item)
            self.tabela.delete(item)
            del self._pontos[idx]

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
        self._gravador_esv = None
        self._gravador_deg = None
        self._deg_estado = None   # dict com o estado do ensaio de degrau em curso
        self._monta()

    def _monta(self):
        esv = ttk.LabelFrame(self, text='Ensaio de esvaziamento (Secao 2.3.2)', padding=10)
        esv.pack(fill='x', pady=(0, 10))
        ttk.Label(
            esv, text='Encha o tanque com o dreno fechado, desligue PUMP2 e S (nos sliders\n'
                      'acima) e clique "iniciar gravacao" no EXATO instante em que abrir o\n'
                      'dreno - esse e o t = 0 do ensaio. So observa; nao atua em nada.\n'
                      'O CSV sai com a coluna h_mm ja em milimetros, pela calibracao de LT\n'
                      'ativa no painel de leituras (a mesma da curva h do grafico).',
            justify='left').grid(row=0, column=0, columnspan=4, sticky='w', pady=(0, 8))

        ttk.Label(esv, text='periodo T (s):').grid(row=1, column=0, sticky='w')
        self.var_esv_T = tk.StringVar(value='2')
        ttk.Entry(esv, textvariable=self.var_esv_T, width=6).grid(
            row=1, column=1, sticky='w', padx=(4, 20))
        ttk.Label(esv, text='arquivo:').grid(row=1, column=2, sticky='w')
        self.var_esv_arquivo = tk.StringVar(value='esvaziamento.csv')
        ttk.Entry(esv, textvariable=self.var_esv_arquivo, width=24).grid(
            row=1, column=3, sticky='w', padx=(4, 0))

        self.bt_esv = ttk.Button(esv, text='iniciar gravacao', command=self._alterna_esv)
        self.bt_esv.grid(row=2, column=0, columnspan=2, sticky='w', pady=(8, 0))
        self.lb_esv = ttk.Label(esv, text='parado.')
        self.lb_esv.grid(row=2, column=2, columnspan=2, sticky='w', pady=(8, 0))

        deg = ttk.LabelFrame(self, text='Ensaio de degrau (Secao 2.3.3)', padding=10)
        deg.pack(fill='x')
        ttk.Label(
            deg, text='Toma o controle de VALVE e PUMP2 durante o ensaio (os sliders ficam\n'
                      'bloqueados). Ao terminar, devolve o controle aos sliders, no ultimo\n'
                      'comando aplicado - sem zerar as saidas. Grava o mesmo CSV do ensaio\n'
                      'de esvaziamento, com h_mm pela calibracao de LT ativa.',
            justify='left').grid(row=0, column=0, columnspan=4, sticky='w', pady=(0, 8))

        campos = (
            ('valve (%)', 'var_deg_valve', '100'),
            ('PUMP2 inicial (%)', 'var_deg_pi', '45'),
            ('PUMP2 final (%)', 'var_deg_pf', '65'),
            ('t do degrau (s)', 'var_deg_tdeg', '10'),
            ('periodo T (s)', 'var_deg_T', '2'),
            ('duracao (s, vazio = ate parar)', 'var_deg_dur', '180'),
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

    # -- ensaio de esvaziamento ---------------------------------------------

    def _alterna_esv(self):
        if self._gravador_esv is None:
            try:
                T = float(self.var_esv_T.get().replace(',', '.'))
            except ValueError:
                messagebox.showerror('T invalido', 'Digite o periodo T, em segundos.')
                return
            if T <= 0:
                messagebox.showerror('T invalido', 'O periodo T tem de ser positivo.')
                return
            if not self.app.confirma_calibracao_lt('ensaio de esvaziamento'):
                return
            caminho = self.var_esv_arquivo.get().strip() or 'esvaziamento.csv'
            self._gravador_esv = GravadorEnsaio(
                caminho, T, pump2_pct_fn=lambda: None,
                h_fn=self.app.contas_para_altura_ativa)
            self.bt_esv.configure(text='parar gravacao')
            self.lb_esv.configure(
                text=f'gravando em {caminho} (h_mm pela calibracao '
                     f'{self.app.rotulo_calibracao_lt()}) ...')
        else:
            n = self._gravador_esv.linhas
            caminho = self._gravador_esv.caminho
            self._gravador_esv.fecha()
            self._gravador_esv = None
            self.bt_esv.configure(text='iniciar gravacao')
            self.lb_esv.configure(text=f'parado. {n} amostras salvas em {caminho}.')

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
        if valve < 100.0 - 1e-6:
            messagebox.showerror(
                'Intertravamento',
                f'valve (%) = {valve:.0f} bloquearia PUMP2 (PUMP2 so e liberado com '
                'VALVE em 100 %). Use valve = 100.')
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
        if self._gravador_esv is not None:
            self._gravador_esv.recebe(t, valores)

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


class AbaAula3(AbaBase):
    """Aula 3 - varredura estatica do atuador e escada de degraus (Secoes 3.3.1 e 3.3.2)."""

    def __init__(self, master, app):
        super().__init__(master, app)
        self._var_estado = None
        self._var_arquivo = None
        self._var_escritor = None
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
                      'que qin(u) nao depende do nivel.\n'
                      'Exige VALVE em 100 % (liberado pelo slider/botao do topo da janela).',
            justify='left').grid(row=0, column=0, columnspan=6, sticky='w', pady=(0, 8))

        campos_var = (
            ('comando inicial (%)', 'var_var_ini', '0'),
            ('comando final (%)', 'var_var_fim', '100'),
            ('passo (%)', 'var_var_passo', '10'),
            ('permanencia por patamar (s)', 'var_var_perm', '15'),
            ('media dos ultimos (s)', 'var_var_media', '5'),
        )
        for i, (rotulo, nome, padrao) in enumerate(campos_var):
            v = tk.StringVar(value=padrao)
            setattr(self, nome, v)
            ttk.Label(var, text=rotulo + ':').grid(row=1 + i // 3, column=2 * (i % 3), sticky='w')
            ttk.Entry(var, textvariable=v, width=8).grid(
                row=1 + i // 3, column=2 * (i % 3) + 1, sticky='w', padx=(4, 20))

        ttk.Label(var, text='arquivo:').grid(row=3, column=0, sticky='w')
        self.var_var_arquivo = tk.StringVar(value='curva_atuador.csv')
        ttk.Entry(var, textvariable=self.var_var_arquivo, width=24).grid(
            row=3, column=1, columnspan=3, sticky='w', padx=(4, 0))

        self.bt_var = ttk.Button(var, text='iniciar varredura', command=self._alterna_var)
        self.bt_var.grid(row=4, column=0, columnspan=2, sticky='w', pady=(8, 0))
        self.lb_var = ttk.Label(var, text='parado.')
        self.lb_var.grid(row=4, column=2, columnspan=4, sticky='w', pady=(8, 0))

        self.tabela_var = ttk.Treeview(
            var, columns=('u', 'sentido', 'qin', 'h'), show='headings', height=5)
        self.tabela_var.heading('u', text='u [%]')
        self.tabela_var.heading('sentido', text='sentido')
        self.tabela_var.heading('qin', text='qin medio [L/min]')
        self.tabela_var.heading('h', text='h medio [mm]')
        self.tabela_var.grid(row=5, column=0, columnspan=6, sticky='nsew', pady=(8, 0))

        esc = ttk.LabelFrame(self, text='Escada de degraus (Secao 3.3.2)', padding=10)
        esc.pack(fill='both', expand=True)
        ttk.Label(
            esc, text='Aplica uma sequencia de comandos de PUMP2, cada um mantido pela mesma\n'
                      'duracao, gravando um CSV continuo (t_s, lt_contas, h_mm, ft2_contas,\n'
                      'qin_lpm, pump2_pct) e, num segundo arquivo, o instante de inicio, o h de\n'
                      'equilibrio e o qin de equilibrio de cada patamar (Tab. 3.2 e Tab. 3.3).\n'
                      'Nos dois arquivos, h sai em mm pela calibracao de LT ativa no painel de\n'
                      'leituras - a mesma da curva h do grafico.',
            justify='left').grid(row=0, column=0, columnspan=6, sticky='w', pady=(0, 8))

        ttk.Label(esc, text='sequencia de comandos (%):').grid(row=1, column=0, sticky='w')
        self.var_esc_seq = tk.StringVar(value='40,60,80,60,100')
        ttk.Entry(esc, textvariable=self.var_esc_seq, width=28).grid(
            row=1, column=1, columnspan=3, sticky='w', padx=(4, 0))

        campos_esc = (
            ('duracao por patamar (s)', 'var_esc_dur', '180'),
            ('periodo T (s)', 'var_esc_T', '1'),
            ('media dos ultimos (s)', 'var_esc_media', '10'),
        )
        for i, (rotulo, nome, padrao) in enumerate(campos_esc):
            v = tk.StringVar(value=padrao)
            setattr(self, nome, v)
            ttk.Label(esc, text=rotulo + ':').grid(row=2, column=2 * i, sticky='w', pady=(6, 0))
            ttk.Entry(esc, textvariable=v, width=8).grid(
                row=2, column=2 * i + 1, sticky='w', padx=(4, 20), pady=(6, 0))

        ttk.Label(esc, text='arquivo:').grid(row=3, column=0, sticky='w')
        self.var_esc_arquivo = tk.StringVar(value='escada_degraus.csv')
        ttk.Entry(esc, textvariable=self.var_esc_arquivo, width=24).grid(
            row=3, column=1, columnspan=3, sticky='w', padx=(4, 0))

        self.bt_esc = ttk.Button(esc, text='iniciar escada', command=self._alterna_esc)
        self.bt_esc.grid(row=4, column=0, columnspan=2, sticky='w', pady=(8, 0))
        self.lb_esc = ttk.Label(esc, text='parado.')
        self.lb_esc.grid(row=4, column=2, columnspan=4, sticky='w', pady=(8, 0))

        self.tabela_esc = ttk.Treeview(
            esc, columns=('patamar', 'u', 't0', 'heq', 'qineq'), show='headings', height=6)
        for coluna, texto in (('patamar', 'patamar'), ('u', 'u [%]'), ('t0', 't inicio [s]'),
                              ('heq', 'h_eq [mm]'), ('qineq', 'qin_eq [L/min]')):
            self.tabela_esc.heading(coluna, text=texto)
        self.tabela_esc.grid(row=5, column=0, columnspan=6, sticky='nsew', pady=(8, 0))
        esc.rowconfigure(5, weight=1)
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
            media = self._le_float(self.var_var_media, 'media dos ultimos (s)', 1e-6)
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
        if not self.app.pede_controle('Aula 3 - varredura estatica'):
            return

        caminho = self.var_var_arquivo.get().strip() or 'curva_atuador.csv'
        self._var_arquivo = open(caminho, 'w', newline='')
        self._var_escritor = csv.writer(self._var_arquivo)
        self._var_escritor.writerow(['u_pct', 'sentido', 'qin_lpm', 'h_mm'])
        for item in self.tabela_var.get_children():
            self.tabela_var.delete(item)

        self._var_estado = {
            'lista': lista, 'idx': 0, 't0': None, 't_inicio_patamar': 0.0,
            'permanencia': perm, 'media_s': media, 'buffer': deque(maxlen=4000),
        }
        self.app.aplica_comando(100.0, lista[0][0])
        self.bt_var.configure(text='parar varredura')
        self.lb_var.configure(text=f'patamar 1/{len(lista)}: PUMP2 -> {lista[0][0]:.0f} % (subida)')

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
            self.tabela_var.insert('', 'end', values=(f'{u_atual:.0f}', sentido_atual,
                                                       f'{qin_medio:.3f}', f'{h_medio:.1f}'))

            idx += 1
            if idx >= len(estado['lista']):
                self._encerra_var('varredura concluida')
                return
            estado['idx'] = idx
            estado['t_inicio_patamar'] = trel
            estado['buffer'].clear()
            u_novo, sentido_novo = estado['lista'][idx]
            self.app.aplica_comando(100.0, u_novo)
            self.lb_var.configure(
                text=f'patamar {idx + 1}/{len(estado["lista"])}: PUMP2 -> {u_novo:.0f} % '
                     f'({sentido_novo})')

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
                                 'Digite comandos separados por virgula, ex.: 40,60,80,60,100.')
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

        if not self.app.confirma_calibracao_lt('escada de degraus'):
            return
        if not self.app.pede_controle('Aula 3 - escada de degraus'):
            return

        caminho = self.var_esc_arquivo.get().strip() or 'escada_degraus.csv'
        base, _ext = os.path.splitext(caminho)
        caminho_eq = base + '_equilibrios.csv'

        self._esc_gravador = GravadorEnsaio(
            caminho, T, pump2_pct_fn=lambda: self._esc_estado['seq'][self._esc_estado['idx']],
            h_fn=self.app.contas_para_altura_ativa)
        self._esc_arquivo_eq = open(caminho_eq, 'w', newline='')
        self._esc_escritor_eq = csv.writer(self._esc_arquivo_eq)
        self._esc_escritor_eq.writerow(['patamar', 'u_pct', 't_inicio_s', 'h_eq_mm', 'qin_eq_lpm'])
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

        if trel - estado['t_inicio_patamar'] >= estado['dur']:
            h_eq = self._janela_media([(tt, hh) for tt, hh, _q in estado['buffer']],
                                      trel, estado['media_s'])
            qin_eq = self._janela_media([(tt, qq) for tt, _h, qq in estado['buffer']],
                                        trel, estado['media_s'])
            idx = estado['idx']
            self._esc_escritor_eq.writerow([
                idx + 1, f'{estado["seq"][idx]:.1f}', f'{estado["t_inicio_patamar"]:.2f}',
                f'{h_eq:.2f}', f'{qin_eq:.4f}',
            ])
            self._esc_arquivo_eq.flush()
            self.tabela_esc.insert('', 'end', values=(
                idx + 1, f'{estado["seq"][idx]:.0f}', f'{estado["t_inicio_patamar"]:.0f}',
                f'{h_eq:.1f}', f'{qin_eq:.3f}'))

            idx += 1
            if idx >= len(estado['seq']):
                self._encerra_esc('sequencia concluida')
                return
            estado['idx'] = idx
            estado['t_inicio_patamar'] = trel
            estado['buffer'].clear()
            u_novo = estado['seq'][idx]
            self.app.aplica_comando(100.0, u_novo)
            self.lb_esc.configure(
                text=f'patamar {idx + 1}/{len(estado["seq"])}: PUMP2 -> {u_novo:.0f} % '
                     f'(degrau em t = {trel:.0f} s)')

    # -- despacho ------------------------------------------------------

    def atualiza_amostra(self, t, valores):
        if self._var_estado is not None:
            self._atualiza_var(t, valores)
        if self._esc_estado is not None and self._esc_gravador is not None:
            self._atualiza_esc(t, valores)


class AbaEmDesenvolvimento(AbaBase):
    """Placeholder para as aulas cujo material ainda nao foi escrito (ver CLAUDE.md)."""

    def __init__(self, master, app, numero):
        super().__init__(master, app)
        ttk.Label(
            self, text=f'Aula {numero}: material ainda em desenvolvimento.\n\n'
                      'Quando o roteiro desta aula estiver pronto, acrescente aqui a '
                      'aba com as ferramentas de ensaio correspondentes (siga o padrao '
                      'de AbaAula1/AbaAula2 neste mesmo arquivo).',
            justify='left', foreground='#666').pack(anchor='nw')


# ---------------------------------------------------------------------------
# Janela principal
# ---------------------------------------------------------------------------

class Janela(tk.Tk):

    def __init__(self, planta, janela_s=JANELA_S):
        super().__init__()
        self.title('Planta TQ CE117 - hub de ensaios')
        self.geometry('980x760')
        self.minsize(760, 600)

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
        comando = ttk.LabelFrame(self, text='Comando manual', padding=10)
        comando.pack(fill='x', padx=10, pady=(10, 6))

        ttk.Label(comando, text='VALVE (S):').grid(row=0, column=0, sticky='w')
        self.var_slider_valve = tk.DoubleVar(value=0.0)
        self.sl_valve = ttk.Scale(
            comando, from_=0, to=100, orient='horizontal',
            variable=self.var_slider_valve, command=self._slider_valve_moveu, length=260)
        self.sl_valve.grid(row=0, column=1, sticky='we', padx=8)
        self.lb_slider_valve = ttk.Label(comando, text='0 %', width=6)
        self.lb_slider_valve.grid(row=0, column=2, sticky='w')
        self.bt_valve = tk.Button(comando, width=14, command=self._alterna_valve_botao)
        self.bt_valve.grid(row=0, column=3, sticky='w', padx=(14, 0))

        ttk.Label(comando, text='PUMP2:').grid(row=1, column=0, sticky='w', pady=(6, 0))
        self.var_slider_pump2 = tk.DoubleVar(value=0.0)
        self.sl_pump2 = ttk.Scale(
            comando, from_=0, to=100, orient='horizontal',
            variable=self.var_slider_pump2, command=self._slider_pump2_moveu, length=260)
        self.sl_pump2.grid(row=1, column=1, sticky='we', padx=8, pady=(6, 0))
        self.lb_slider_pump2 = ttk.Label(comando, text='0 %', width=6)
        self.lb_slider_pump2.grid(row=1, column=2, sticky='w', pady=(6, 0))
        self.bt_pump2 = tk.Button(comando, width=14, command=self._alterna_pump2_botao)
        self.bt_pump2.grid(row=1, column=3, sticky='w', padx=(14, 0), pady=(6, 0))

        ttk.Checkbutton(comando, text='zerar saidas ao sair',
                        variable=self.zerar_ao_sair).grid(
            row=0, column=4, rowspan=2, sticky='e', padx=(20, 0))
        comando.columnconfigure(1, weight=1)
        comando.columnconfigure(4, weight=1)

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

        # Altura generosa por padrao; o proprio `Panedwindow` deixa o usuario
        # arrastar a divisoria para dar ainda mais (ou menos) espaco ao
        # grafico em relacao as abas logo abaixo.
        self.gr = Grafico(quadro_grafico, janela_s=self.janela_s, height=320)
        self.gr.pack(fill='both', expand=True, pady=(6, 0))
        painel_topo.add(quadro_grafico, weight=3)

        painel.add(painel_topo, weight=3)

        self.notebook = ttk.Notebook(painel)
        painel.add(self.notebook, weight=2)

        self._adiciona_aba('Aula 1', AbaAula1)
        self._adiciona_aba('Aula 2', AbaAula2)
        self._adiciona_aba('Aula 3', AbaAula3)
        for n in range(4, 8):
            self._adiciona_aba(f'Aula {n}', AbaEmDesenvolvimento, n)

        self.lb_status = ttk.Label(self, text='iniciando...', anchor='w',
                                   relief='sunken', padding=(6, 3))
        self.lb_status.pack(fill='x', padx=10, pady=(0, 10))

        self._atualiza_controles()

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

    def _valve_totalmente_aberta(self):
        return self.valve_pct >= 100.0 - 1e-6

    def _atualiza_controles(self):
        automatico = self.controle_owner is not None
        estado = 'disabled' if automatico else 'normal'
        self.sl_valve.configure(state=estado)
        # PUMP2 so libera com VALVE em 100 % (S totalmente aberta antes de PUMP2).
        pump2_liberado = not automatico and self._valve_totalmente_aberta()
        self.sl_pump2.configure(state='normal' if pump2_liberado else 'disabled')

        self._pinta_botao(self.bt_valve, 'VALVE (S)', self.valve_pct, habilitado=not automatico)
        self._pinta_botao(self.bt_pump2, 'PUMP2', self.pump2_pct, habilitado=pump2_liberado,
                          motivo_bloqueio='abra VALVE em 100 % antes' if not automatico else None)

        if automatico:
            self.lb_status.configure(
                text=f'controle automatico: {self.controle_owner}', foreground='#a11')

    def _pinta_botao(self, botao, nome, pct, habilitado, motivo_bloqueio=None):
        ligado = pct >= 100.0 - 1e-6
        if not habilitado and motivo_bloqueio and not ligado:
            rotulo = f'{nome}\nOFF - {motivo_bloqueio}'
        else:
            rotulo = f'{nome}\n{"ON" if ligado else "OFF"}  ({pct:.0f} %)'
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

        Intertravamento: PUMP2 so e aceito com VALVE em 100 % (S totalmente
        aberta) - nao basta S estar parcialmente aberta.
        """
        valve_pct = max(0.0, min(100.0, valve_pct))
        pump2_pct = max(0.0, min(100.0, pump2_pct))
        if valve_pct < 100.0 - 1e-6:
            pump2_pct = 0.0
        self.valve_pct, self.pump2_pct = valve_pct, pump2_pct

        self.aquisicao.setpoint(
            percentual_para_conta(valve_pct), percentual_para_conta(pump2_pct))

        # reflete nos sliders sem disparar de volta o callback de comando
        self.var_slider_valve.set(valve_pct)
        self.var_slider_pump2.set(pump2_pct)
        self.lb_slider_valve.configure(text=f'{valve_pct:.0f} %')
        self.lb_slider_pump2.configure(text=f'{pump2_pct:.0f} %')
        self._atualiza_controles()

    def _slider_valve_moveu(self, _valor):
        if self.controle_owner is not None:
            return
        self.aplica_comando(self.var_slider_valve.get(), self.pump2_pct)

    def _slider_pump2_moveu(self, _valor):
        if self.controle_owner is not None:
            return
        if not self._valve_totalmente_aberta():
            self.var_slider_pump2.set(0.0)
            messagebox.showwarning(
                'Intertravamento',
                'PUMP2 bloqueado: abra a valvula S em 100 % antes de acionar a '
                'bomba (a bomba contra a valvula parcial ou totalmente fechada '
                'pressuriza a linha).')
            return
        self.aplica_comando(self.valve_pct, self.var_slider_pump2.get())

    def _alterna_valve_botao(self):
        if self.controle_owner is not None:
            return
        novo = 0.0 if self._valve_totalmente_aberta() else 100.0
        self.aplica_comando(novo, self.pump2_pct)

    def _alterna_pump2_botao(self):
        if self.controle_owner is not None:
            return
        if not self._valve_totalmente_aberta():
            messagebox.showwarning(
                'Intertravamento',
                'PUMP2 bloqueado: abra a valvula S em 100 % antes de acionar a '
                'bomba (a bomba contra a valvula parcial ou totalmente fechada '
                'pressuriza a linha).')
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
        if ok_pdf:
            resumo += f'\nPDF: {caminho_pdf}'
        else:
            resumo += f'\n\nPDF nao gerado ({erro_pdf}).'
        messagebox.showinfo('Exportacao concluida', resumo)
        self.lb_status.configure(
            text=f'exportado: {len(linhas)} amostras.', foreground='#0a7d32')

    def _salva_csv_exportacao(self, caminho, linhas):
        t0 = linhas[0][0]
        with open(caminho, 'w', newline='') as arquivo:
            escritor = csv.writer(arquivo)
            escritor.writerow(COLUNAS_EXPORTACAO)
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
                ])

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
