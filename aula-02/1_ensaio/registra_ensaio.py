"""Registra um ensaio da planta TQ CE117 num CSV com marcacao de tempo.

Implementa o laco de amostragem de periodo fixo T do Cod. 2.1 da Aula 2: a
cada iteracao le LT_ADC e FT2_ADC, converte para h [mm] e q_in [L/min] com
`conversoes.py`, grava a linha no CSV e dorme o tempo que sobrou do periodo.

Dois usos, correspondentes aos dois ensaios da aula:

  Esvaziamento (Secao 2.3.2) - so grava, nao atua em nada. Inicie a gravacao
  no exato instante em que abrir a valvula de dreno:

      python3 -W ignore 1_ensaio/registra_ensaio.py -T 2 -o esvaziamento.csv

  Degrau na vazao de entrada (Secao 2.3.3) - mantem PUMP2 no comando inicial,
  aplica o degrau no instante programado e continua gravando:

      python3 -W ignore 1_ensaio/registra_ensaio.py --degrau \
          --pump2-inicial 45 --pump2-final 65 --t-degrau 10 \
          -T 2 -d 180 -o degrau.csv

Encerre a qualquer momento com Ctrl+C: o CSV ja esta gravado ate a ultima
amostra. Sem `-d`, o programa grava indefinidamente ate o Ctrl+C.

A opcao `--sim` simula o tanque (Torricelli) sem tocar no CLP, para testar o
programa longe do laboratorio.
"""

import argparse
import csv
import math
import os
import sys
import time

# `conversoes.py` fica em `comum/`, na raiz do repositorio: dois niveis acima
# da pasta deste script.
RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(RAIZ, 'comum'))

from conversoes import (contas_para_altura, contas_para_vazao,
                        percentual_para_conta)

PLC_IP = '200.200.200.25'

TAG_LT = 'Program:MainProgram.LT_ADC'
TAG_FT2 = 'Program:MainProgram.FT2_ADC'
TAG_VALVE = 'Program:MainProgram.VALVE_DAC'
TAG_PUMP2 = 'Program:MainProgram.PUMP2_DAC'

COLUNAS = ['t_s', 'lt_contas', 'h_mm', 'ft2_contas', 'qin_lpm', 'pump2_pct']


# ---------------------------------------------------------------------------
# Acesso a planta: uma classe para o CLP real, outra para o tanque simulado.
# Ambas expoem o mesmo par de metodos, de modo que o laco principal nao
# precisa saber com qual delas esta falando.
# ---------------------------------------------------------------------------

class PlantaCLP:
    """Sessao CIP com o CLP, via pycomm3."""

    def __init__(self, ip):
        from pycomm3 import LogixDriver
        self._plc = LogixDriver(ip)
        self._plc.open()
        print(f'Conectado em {ip} ({self._plc.info.get("product_name")})')

    def le(self):
        """Devolve (lt_contas, ft2_contas) numa unica requisicao CIP."""
        lt, ft2 = self._plc.read(TAG_LT, TAG_FT2)
        if lt.error or ft2.error:
            raise RuntimeError(f'erro de leitura: {lt.error or ft2.error}')
        return lt.value, ft2.value

    def comanda(self, valve_pct, pump2_pct):
        """Escreve os dois DACs. A valvula sempre antes da bomba."""
        resultados = self._plc.write(
            (TAG_VALVE, percentual_para_conta(valve_pct)),
            (TAG_PUMP2, percentual_para_conta(pump2_pct)),
        )
        for r in resultados:
            if r.error:
                raise RuntimeError(f'erro ao escrever em {r.tag}: {r.error}')

    def fecha(self):
        self._plc.close()


class PlantaSimulada:
    """Tanque de Torricelli: A dh/dt = q_in - k sqrt(h).

    Serve so para exercitar o programa fora do laboratorio; os parametros
    abaixo nao sao medidos, mas foram escolhidos para reproduzir os numeros do
    ensaio piloto da Aula 2: equilibrio em ~180 mm com PUMP2 e S em 100 %,
    esvaziamento de 180 mm em ~50 s e tau ~ 20 s no ponto de operacao
    h0 = 30 mm.
    """

    AREA_MM2 = 1.02e4         # ~ tanque de 114 mm de diametro
    K = 5.47e3                # mm^2.5/s: 2 A sqrt(180) / 50 s
    VAZAO_MAX_MM3_S = 7.34e4  # ~ 4,4 L/min: K sqrt(180), o equilibrio a 100 %

    def __init__(self):
        self.h = 0.0
        self.valve_pct = 0.0
        self.pump2_pct = 0.0
        self._t_anterior = time.time()

    def le(self):
        agora = time.time()
        dt = agora - self._t_anterior
        self._t_anterior = agora

        q_in = (self.pump2_pct / 100.0) * (self.valve_pct / 100.0) * self.VAZAO_MAX_MM3_S
        q_out = self.K * math.sqrt(max(0.0, self.h))
        self.h = max(0.0, self.h + dt * (q_in - q_out) / self.AREA_MM2)

        from conversoes import LT_A, LT_B, volts_para_conta, FT2_LPM_POR_VOLT
        lt_contas = volts_para_conta((self.h - LT_B) / LT_A)
        q_lpm = q_in * 60.0 / 1.0e6
        ft2_contas = volts_para_conta(q_lpm / FT2_LPM_POR_VOLT)
        return lt_contas, ft2_contas

    def comanda(self, valve_pct, pump2_pct):
        self.valve_pct, self.pump2_pct = valve_pct, pump2_pct

    def fecha(self):
        pass


# ---------------------------------------------------------------------------

def le_argumentos():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('-T', '--periodo', type=float, default=1.0,
                   help='periodo de amostragem, em segundos (padrao: 1.0)')
    p.add_argument('-d', '--duracao', type=float, default=None,
                   help='duracao do ensaio, em segundos (padrao: ate Ctrl+C)')
    p.add_argument('-o', '--saida', default='ensaio.csv',
                   help='arquivo CSV de saida (padrao: ensaio.csv)')
    p.add_argument('--ip', default=PLC_IP, help=f'IP do CLP (padrao: {PLC_IP})')
    p.add_argument('--sim', action='store_true',
                   help='simula o tanque, sem tocar no CLP')

    g = p.add_argument_group('ensaio de degrau (Secao 2.3.3)')
    g.add_argument('--degrau', action='store_true',
                   help='aplica um degrau no comando de PUMP2 durante a gravacao')
    g.add_argument('--pump2-inicial', type=float,
                   help='comando de PUMP2 antes do degrau, em %% (o do ponto de operacao)')
    g.add_argument('--pump2-final', type=float,
                   help='comando de PUMP2 depois do degrau, em %%')
    g.add_argument('--t-degrau', type=float, default=10.0,
                   help='instante do degrau, em segundos desde o inicio (padrao: 10)')
    g.add_argument('--valve', type=float, default=100.0,
                   help='abertura da valvula S durante o ensaio, em %% (padrao: 100)')

    args = p.parse_args()

    if args.periodo <= 0:
        p.error('o periodo de amostragem tem de ser positivo')
    if args.degrau:
        if args.pump2_inicial is None or args.pump2_final is None:
            p.error('--degrau exige --pump2-inicial e --pump2-final')
        for nome, valor in (('--pump2-inicial', args.pump2_inicial),
                            ('--pump2-final', args.pump2_final),
                            ('--valve', args.valve)):
            if not 0.0 <= valor <= 100.0:
                p.error(f'{nome} = {valor} fora da faixa [0, 100] %')
        if args.duracao is not None and args.duracao <= args.t_degrau:
            p.error('a duracao tem de ser maior que --t-degrau, senao o degrau '
                    'nunca chega a ser aplicado')
    return args


def main():
    args = le_argumentos()
    planta = PlantaSimulada() if args.sim else PlantaCLP(args.ip)

    # Intertravamento de seguranca da Aula 1: a valvula S abre ANTES da bomba.
    # No ensaio de esvaziamento nao ha atuacao nenhuma, e a planta e apenas
    # observada.
    pump2_pct = ''
    if args.degrau:
        pump2_pct = args.pump2_inicial
        print(f'Abrindo VALVE = {args.valve:.0f} % e ajustando '
              f'PUMP2 = {pump2_pct:.0f} % (condicao inicial)')
        planta.comanda(args.valve, pump2_pct)

    print(f'Gravando em {args.saida} com T = {args.periodo:g} s. Ctrl+C encerra.')
    if args.degrau:
        print(f'Degrau de PUMP2 {args.pump2_inicial:.0f} % -> '
              f'{args.pump2_final:.0f} % programado para t = {args.t_degrau:g} s.')

    degrau_aplicado = False
    atrasos = 0

    with open(args.saida, 'w', newline='') as arquivo:
        escritor = csv.writer(arquivo)
        escritor.writerow(COLUNAS)

        t0 = time.time()
        try:
            while True:
                inicio_iteracao = time.time()
                t = inicio_iteracao - t0

                if args.duracao is not None and t > args.duracao:
                    break

                # Degrau: aplicado antes da leitura da propria amostra, para
                # que o instante do comando fique registrado no CSV.
                if args.degrau and not degrau_aplicado and t >= args.t_degrau:
                    pump2_pct = args.pump2_final
                    planta.comanda(args.valve, pump2_pct)
                    degrau_aplicado = True
                    print(f'--> t = {t:6.2f} s: degrau aplicado, '
                          f'PUMP2 = {pump2_pct:.0f} %')

                lt_contas, ft2_contas = planta.le()
                h = contas_para_altura(lt_contas)
                q_in = contas_para_vazao(ft2_contas)

                escritor.writerow([f'{t:.3f}', lt_contas, f'{h:.3f}',
                                   ft2_contas, f'{q_in:.4f}', pump2_pct])
                arquivo.flush()  # o CSV fica utilizavel mesmo se der Ctrl+C
                print(f't = {t:7.2f} s   h = {h:7.2f} mm   q_in = {q_in:6.3f} L/min')

                # Desconta o tempo ja gasto na iteracao antes de dormir: sem
                # isso, o periodo efetivo seria T + latencia de comunicacao.
                sobra = args.periodo - (time.time() - inicio_iteracao)
                if sobra < 0:
                    atrasos += 1
                time.sleep(max(0.0, sobra))
        except KeyboardInterrupt:
            print('\nEnsaio interrompido pelo usuario.')
        finally:
            planta.fecha()

    print(f'Amostras gravadas em {args.saida}.')
    if atrasos:
        print(f'ATENCAO: {atrasos} iteracao(oes) demoraram mais que T = '
              f'{args.periodo:g} s. O periodo efetivo nesses instantes foi maior '
              f'que o programado - considere aumentar T.')


if __name__ == '__main__':
    main()
