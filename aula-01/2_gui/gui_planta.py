"""GUI simples para operar a planta TQ CE117 (CLP Allen-Bradley / EtherNet-IP).

E a interface da Secao 1.3.4 do roteiro da Aula 1. Permite:
  - ligar/desligar PUMP2 e S em dois botoes ON-OFF (OFF = 0 %, ON = 100 %);
  - acompanhar LT em tempo real, em contas e em volts, que sao os dois valores
    anotados na Tab. 1.5 (calibracao de LT) ao lado da leitura da regua;
  - limpar o grafico entre a curva de subida e a de descida do nivel.

Intertravamento (OBS da Secao 1.2.3): a valvula S tem de estar aberta antes de
qualquer acionamento de PUMP2 - acionar a bomba contra a valvula fechada
pressuriza a linha e pode danificar a planta. O CLP nao implementa essa trava;
quem a implementa e este programa, que recusa ligar PUMP2 com S fechada e
desliga PUMP2 junto se S for fechada.

Uso (a partir da pasta aula-01/):
    python3 -W ignore 2_gui/gui_planta.py
    python3 -W ignore 2_gui/gui_planta.py --sim   (sem CLP, dados falsos)
    python3 -W ignore 2_gui/gui_planta.py --ip 200.200.200.25
    python3 -W ignore 2_gui/gui_planta.py --janela 300  (janela inicial de 5 min)

Requisito: tkinter (no Ubuntu: sudo apt install python3-tk).
"""

import argparse
import os
import queue
import sys
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import ttk

# `conversoes.py` fica em `comum/`, na raiz do repositorio: dois niveis acima
# da pasta deste script.
RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(RAIZ, 'comum'))

from conversoes import (V_FUNDO_CARTAO, conta_para_percentual,
                        conta_para_volts, percentual_para_conta)

PLC_IP = '200.200.200.25'

PERIODO_S = 0.5      # intervalo entre leituras
JANELA_S = 60.0      # largura inicial da janela de tempo do grafico
# opcoes oferecidas na GUI: (rotulo, segundos)
JANELAS = (('30 s', 30.0), ('1 min', 60.0), ('2 min', 120.0), ('5 min', 300.0),
           ('10 min', 600.0), ('30 min', 1800.0))
JANELA_MAX_S = max(s for _r, s in JANELAS)   # historico guardado em memoria
DAC_MAX = 32767      # 2**15 - 1: o maior valor que cabe num INT do Logix

TAG_PUMP2 = 'Program:MainProgram.PUMP2_DAC'
TAG_VALVE = 'Program:MainProgram.VALVE_DAC'
TAG_LT = 'Program:MainProgram.LT_ADC'


def rotulo_janela(segundos):
    """Rotulo da janela: usa o nome da opcao conhecida, senao mostra os segundos."""
    for rotulo, valor in JANELAS:
        if abs(valor - segundos) < 1e-6:
            return rotulo
    return f'{segundos:.0f} s'


def rotulo_tempo(t):
    """Rotulo do eixo x: segundos ate 2 min, mm:ss dai em diante."""
    if t < 120:
        return f'{t:.0f}s'
    return f'{int(t) // 60:d}:{int(t) % 60:02d}'


# ---------------------------------------------------------------------------
# Acesso a planta
# ---------------------------------------------------------------------------

class PlantaCLP:
    """Le e escreve as tags do CLP real."""

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
        for r in self.plc.write((TAG_VALVE, valve), (TAG_PUMP2, pump2)):
            if r.error:
                raise RuntimeError(f'escrita em {r.tag}: {r.error}')

    def le(self):
        """Devolve (lt, pump2, valve) em contas."""
        leituras = {}
        for r in self.plc.read(TAG_LT, TAG_PUMP2, TAG_VALVE):
            if r.error:
                raise RuntimeError(f'leitura de {r.tag}: {r.error}')
            leituras[r.tag] = r.value
        return leituras[TAG_LT], leituras[TAG_PUMP2], leituras[TAG_VALVE]


class PlantaSimulada:
    """Tanque de brincadeira, para testar a GUI sem o CLP na mesa."""

    def __init__(self, ip=None):
        self.nivel = 8000.0
        self.pump2 = 0
        self.valve = 0
        self.t_ultimo = time.monotonic()

    def conecta(self):
        return 'SIMULADOR (sem CLP)'

    def fecha(self):
        pass

    def escreve(self, valve, pump2):
        self.valve, self.pump2 = valve, pump2

    def le(self):
        agora = time.monotonic()
        dt, self.t_ultimo = agora - self.t_ultimo, agora

        entra = 0.22 * self.pump2
        sai = 0.14 * self.valve * (self.nivel / DAC_MAX) ** 0.5
        self.nivel += (entra - sai - 120.0) * dt
        self.nivel = min(max(self.nivel, 0.0), float(DAC_MAX))
        return int(self.nivel), self.pump2, self.valve


class Aquisicao(threading.Thread):
    """Thread unica dona da conexao: escreve o setpoint e le as tags em ciclo.

    A GUI so conversa com ela por `setpoint()` e pela fila de eventos --
    o driver do pycomm3 nao e seguro para uso concorrente.
    """

    def __init__(self, planta, fila):
        super().__init__(daemon=True)
        self.planta = planta
        self.fila = fila
        self.parar = threading.Event()
        self.zerar_ao_sair = True

        self._lock = threading.Lock()
        self._alvo = (0, 0)          # (valve, pump2) em contas
        self._pendente = True        # forca a primeira escrita

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

                    lt, pump2, valve = self.planta.le()
                    self.fila.put(('amostra', time.monotonic() - t0, lt, pump2, valve))

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
# Grafico
# ---------------------------------------------------------------------------

class Grafico(tk.Canvas):
    """Linha de LT no tempo, desenhada a mao (sem matplotlib)."""

    MARGEM = (58, 12, 12, 30)   # esquerda, topo, direita, base

    def __init__(self, master, janela_s=JANELA_S, **kw):
        super().__init__(master, background='white', highlightthickness=1,
                         highlightbackground='#b0b0b0', **kw)
        self.pontos = deque()
        self.janela_s = float(janela_s)
        self.bind('<Configure>', lambda _e: self.redesenha())

    def define_janela(self, janela_s):
        """Troca a largura da janela de tempo e redesenha."""
        self.janela_s = float(janela_s)
        self._descarta_velhos()
        self.redesenha()

    def _descarta_velhos(self):
        """Guarda sempre o maior historico oferecido, nao so a janela atual.

        Assim, ao alargar a janela, os dados antigos reaparecem.
        """
        if not self.pontos:
            return
        t = self.pontos[-1][0]
        limite = max(self.janela_s, JANELA_MAX_S)
        while self.pontos and t - self.pontos[0][0] > limite:
            self.pontos.popleft()

    def _visiveis(self):
        """Pontos dentro da janela de tempo atual."""
        if not self.pontos:
            return []
        t_fim = self.pontos[-1][0]
        return [(t, v) for t, v in self.pontos if t >= t_fim - self.janela_s]

    def acrescenta(self, t, volts):
        self.pontos.append((t, volts))
        self._descarta_velhos()

    def limpa(self):
        self.pontos.clear()
        self.redesenha()

    def _faixa_y(self, pontos):
        valores = [v for _t, v in pontos]
        if not valores:
            return 0.0, V_FUNDO_CARTAO
        lo, hi = min(valores), max(valores)
        if hi - lo < 0.5:                       # nao deixa a escala colapsar
            meio = (hi + lo) / 2
            lo, hi = meio - 0.25, meio + 0.25
        folga = (hi - lo) * 0.1
        return lo - folga, hi + folga

    def redesenha(self):
        self.delete('all')
        esq, topo, dir_, base = self.MARGEM
        larg, alt = self.winfo_width(), self.winfo_height()
        x0, y0 = esq, topo
        x1, y1 = larg - dir_, alt - base
        if x1 - x0 < 40 or y1 - y0 < 40:
            return

        pontos = self._visiveis()
        t_fim = pontos[-1][0] if pontos else self.janela_s
        t_ini = max(0.0, t_fim - self.janela_s)
        if t_fim - t_ini < 1.0:
            t_fim = t_ini + 1.0
        v_lo, v_hi = self._faixa_y(pontos)

        def px(t):
            return x0 + (t - t_ini) / (t_fim - t_ini) * (x1 - x0)

        def py(v):
            return y1 - (v - v_lo) / (v_hi - v_lo) * (y1 - y0)

        # grade + rotulos
        for i in range(6):
            v = v_lo + (v_hi - v_lo) * i / 5
            y = py(v)
            self.create_line(x0, y, x1, y, fill='#e8e8e8')
            self.create_text(x0 - 6, y, text=f'{v:.2f}', anchor='e',
                             font=('TkDefaultFont', 8), fill='#555')
        for i in range(5):
            t = t_ini + (t_fim - t_ini) * i / 4
            x = px(t)
            self.create_line(x, y0, x, y1, fill='#f2f2f2')
            self.create_text(x, y1 + 6, text=rotulo_tempo(t), anchor='n',
                             font=('TkDefaultFont', 8), fill='#555')

        self.create_rectangle(x0, y0, x1, y1, outline='#909090')
        self.create_text(x0 - 6, y0 - 4, text='LT (V)', anchor='se',
                         font=('TkDefaultFont', 8, 'bold'), fill='#333')

        if len(pontos) >= 2:
            traco = []
            for t, v in pontos:
                traco += [px(t), py(v)]
            self.create_line(*traco, fill='#0a6ebd', width=2)
            self.create_oval(traco[-2] - 3, traco[-1] - 3,
                             traco[-2] + 3, traco[-1] + 3,
                             fill='#0a6ebd', outline='')


# ---------------------------------------------------------------------------
# Janela
# ---------------------------------------------------------------------------

class Janela(tk.Tk):

    def __init__(self, planta, janela_s=JANELA_S):
        super().__init__()
        self.title('Planta TQ CE117 - operacao')
        self.geometry('760x520')
        self.minsize(620, 440)

        self.fila = queue.Queue()
        self.aquisicao = Aquisicao(planta, self.fila)

        self.pump2_ligado = False
        self.valve_aberta = False
        self.zerar_ao_sair = tk.BooleanVar(value=True)
        self.janela_s = float(janela_s)
        # opcoes do seletor; um --janela fora da lista entra como opcao extra
        self.janelas = dict(JANELAS)
        self.janelas.setdefault(rotulo_janela(self.janela_s), self.janela_s)
        self.janela_txt = tk.StringVar(value=rotulo_janela(self.janela_s))

        self._monta()
        self.protocol('WM_DELETE_WINDOW', self.encerra)

        self.aquisicao.start()
        self.after(100, self._drena_fila)

    # -- construcao da tela -------------------------------------------------

    def _monta(self):
        comandos = ttk.LabelFrame(self, text='Comando', padding=10)
        comandos.pack(fill='x', padx=10, pady=(10, 6))

        self.bt_pump2 = tk.Button(comandos, width=22, height=2,
                                  command=self.alterna_pump2)
        self.bt_pump2.grid(row=0, column=0, padx=(0, 10))

        self.bt_valve = tk.Button(comandos, width=22, height=2,
                                  command=self.alterna_valve)
        self.bt_valve.grid(row=0, column=1, padx=(0, 10))

        ttk.Checkbutton(comandos, text='zerar saidas ao sair',
                        variable=self.zerar_ao_sair).grid(row=0, column=2, sticky='w')
        comandos.columnconfigure(2, weight=1)

        medidas = ttk.LabelFrame(self, text='Leitura do CLP', padding=10)
        medidas.pack(fill='x', padx=10, pady=6)

        self.lb_lt = ttk.Label(medidas, text='LT:  --', font=('TkDefaultFont', 13, 'bold'))
        self.lb_lt.grid(row=0, column=0, sticky='w')
        self.lb_eco = ttk.Label(medidas, text='PUMP2_DAC: --      VALVE_DAC: --')
        self.lb_eco.grid(row=1, column=0, sticky='w', pady=(4, 0))

        janela = ttk.Frame(medidas)
        janela.grid(row=0, column=1, rowspan=2, sticky='e')
        ttk.Label(janela, text='janela:').pack(side='left', padx=(0, 4))
        self.cb_janela = ttk.Combobox(
            janela, textvariable=self.janela_txt, width=8, state='readonly',
            values=sorted(self.janelas, key=self.janelas.get))
        self.cb_janela.pack(side='left', padx=(0, 10))
        self.cb_janela.bind('<<ComboboxSelected>>', self._troca_janela)
        ttk.Button(janela, text='limpar grafico',
                   command=lambda: self.gr.limpa()).pack(side='left')
        medidas.columnconfigure(1, weight=1)

        self.gr = Grafico(self, janela_s=self.janela_s)
        self.gr.pack(fill='both', expand=True, padx=10, pady=6)

        self.lb_status = ttk.Label(self, text='iniciando...', anchor='w',
                                   relief='sunken', padding=(6, 3))
        self.lb_status.pack(fill='x', padx=10, pady=(0, 10))

        self._pinta_botoes()

    def _pinta_botoes(self):
        for botao, nome, ligado in ((self.bt_pump2, 'PUMP2', self.pump2_ligado),
                                    (self.bt_valve, 'S (VALVE)', self.valve_aberta)):
            pct = 100 if ligado else 0
            rotulo = f'{nome}\n{"ON" if ligado else "OFF"}  ({pct} %)'
            if botao is self.bt_pump2 and not self.valve_aberta:
                rotulo = f'{nome}\nOFF  - abra S antes'
            botao.configure(
                text=rotulo,
                background='#1e9e4a' if ligado else '#c9ccd1',
                activebackground='#26b356' if ligado else '#d8dbe0',
                foreground='white' if ligado else 'black',
            )

    # -- acoes --------------------------------------------------------------

    def _troca_janela(self, _evento=None):
        segundos = self.janelas.get(self.janela_txt.get())
        if segundos is None:
            return
        self.janela_s = segundos
        self.gr.define_janela(segundos)
        self.cb_janela.selection_clear()

    def _envia_setpoint(self, aviso=None):
        # A ordem do par e (VALVE, PUMP2): a valvula vai sempre na frente.
        self.aquisicao.setpoint(
            percentual_para_conta(100 if self.valve_aberta else 0),
            percentual_para_conta(100 if self.pump2_ligado else 0),
        )
        self._pinta_botoes()
        if aviso:
            self.lb_status.configure(text=aviso, foreground='#a11')
        else:
            self.lb_status.configure(
                text=f'S {"aberta" if self.valve_aberta else "fechada"}   |   '
                     f'PUMP2 {"ligado" if self.pump2_ligado else "desligado"}',
                foreground='#333')

    def alterna_pump2(self):
        # Intertravamento da OBS da Secao 1.2.3: PUMP2 so liga com S aberta.
        if not self.pump2_ligado and not self.valve_aberta:
            self.lb_status.configure(
                text='PUMP2 bloqueado: abra a valvula S antes de acionar a bomba '
                     '(a bomba contra a valvula fechada pressuriza a linha).',
                foreground='#a11')
            return
        self.pump2_ligado = not self.pump2_ligado
        self._envia_setpoint()

    def alterna_valve(self):
        self.valve_aberta = not self.valve_aberta
        aviso = None
        if not self.valve_aberta and self.pump2_ligado:
            # Fechar S com a bomba ligada e a mesma condicao proibida, vista do
            # outro lado: desliga PUMP2 junto.
            self.pump2_ligado = False
            aviso = 'S fechada: PUMP2 desligado junto, pelo intertravamento.'
        self._envia_setpoint(aviso)

    # -- atualizacao --------------------------------------------------------

    def _drena_fila(self):
        try:
            while True:
                evento = self.fila.get_nowait()
                if evento[0] == 'amostra':
                    _, t, lt, pump2, valve = evento
                    volts = conta_para_volts(lt)
                    # As duas colunas da Tab. 1.5 que saem do CLP: contas e V.
                    # O percentual e do fundo de escala do INSTRUMENTO (10 V =
                    # 31207 contas), nao do cartao (10,5 V = 32767).
                    self.lb_lt.configure(
                        text=f'LT:  {lt:6d} contas   =  {volts:+6.3f} V'
                             f'   ({conta_para_percentual(lt):5.1f} % do fundo)')
                    self.lb_eco.configure(
                        text=f'PUMP2_DAC: {pump2:6d} ({conta_para_percentual(pump2):5.1f} %)'
                             f'      VALVE_DAC: {valve:6d} ({conta_para_percentual(valve):5.1f} %)')
                    self.gr.acrescenta(t, volts)
                    self.gr.redesenha()
                else:
                    _, texto, ok = evento
                    self.lb_status.configure(text=texto,
                                             foreground='#0a7d32' if ok else '#a11')
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
