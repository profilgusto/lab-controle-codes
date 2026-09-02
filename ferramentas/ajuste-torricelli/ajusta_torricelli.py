"""Estima a constante k da lei de Torricelli a partir de um ensaio de esvaziamento.

Durante o esvaziamento livre (q_in = 0), a Eq. (3) da Aula 2

    A dh/dt = -k sqrt(h)

tem solucao analitica (Eq. 11)

    sqrt(h(t)) = sqrt(h(0)) - (k / 2A) t,

ou seja, sqrt(h) decai LINEARMENTE no tempo, mesmo h(t) nao sendo linear. Este
programa le o CSV do ensaio de esvaziamento gerado pela aba "Aula 2" de
`ensaios-gui/hub_planta.py`, calcula sqrt(h) ponto a ponto, ajusta uma reta
por minimos quadrados com `numpy.polyfit` e devolve k = -2 A a, onde `a` e o
coeficiente angular ajustado.

A area A vem da medida de circunferencia da Secao 2.3.1 (D = C/pi,
A = pi D^2/4); informe a circunferencia media, o diametro ou a propria area:

    python3 ajuste-torricelli/ajusta_torricelli.py esvaziamento.csv --circunferencia 358
    python3 ajuste-torricelli/ajusta_torricelli.py esvaziamento.csv --diametro 114
    python3 ajuste-torricelli/ajusta_torricelli.py esvaziamento.csv --area 10207

Perto de h = 0 o esvaziamento desacelera e a leitura de LT perde resolucao;
use `--t-max` (e, se preciso, `--h-min`) para ajustar a reta so no trecho
confiavel do ensaio:

    python3 ajuste-torricelli/ajusta_torricelli.py esvaziamento.csv -c 358 --t-max 55

Sem `--sem-grafico`, abre o grafico de sqrt(h) contra t com a reta ajustada
sobreposta; `--figura arquivo.png` salva em vez de abrir.
"""

import argparse
import csv
import math
import sys

import numpy as np


def le_csv(caminho):
    """Le as colunas t_s e h_mm do CSV do ensaio."""
    t, h = [], []
    with open(caminho, newline='') as arquivo:
        leitor = csv.DictReader(arquivo)
        if leitor.fieldnames is None or 't_s' not in leitor.fieldnames \
                or 'h_mm' not in leitor.fieldnames:
            sys.exit(f'ERRO: {caminho} nao tem as colunas t_s e h_mm. '
                     'Ele foi gerado pelo ensaio de esvaziamento da aba '
                     '"Aula 2" de ensaios-gui/hub_planta.py?')
        for linha in leitor:
            t.append(float(linha['t_s']))
            h.append(float(linha['h_mm']))
    if not t:
        sys.exit(f'ERRO: {caminho} nao tem nenhuma amostra.')
    return np.array(t), np.array(h)


def area_da_secao(args):
    """Devolve a area da secao transversal do tanque, em mm^2."""
    if args.area is not None:
        return args.area
    diametro = args.diametro
    if diametro is None:
        # A circunferencia e medida por fora; se a espessura da parede for
        # informada, desconta-a para obter o diametro interno.
        diametro = args.circunferencia / math.pi - 2.0 * args.parede
    return math.pi * diametro ** 2 / 4.0


def ajusta(t, h, area):
    """Ajusta sqrt(h) = b + a t e devolve (k, a, b, r2)."""
    raiz_h = np.sqrt(h)
    a, b = np.polyfit(t, raiz_h, 1)

    residuos = raiz_h - (a * t + b)
    variancia = np.sum((raiz_h - raiz_h.mean()) ** 2)
    r2 = 1.0 - np.sum(residuos ** 2) / variancia if variancia > 0 else float('nan')

    k = -2.0 * area * a  # a e negativo no esvaziamento, logo k > 0
    return k, a, b, r2


def desenha(t, h, a, b, arquivo):
    import matplotlib
    if arquivo:
        matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    fig, (ax_raiz, ax_h) = plt.subplots(2, 1, figsize=(7, 7), sharex=True)

    ax_raiz.plot(t, np.sqrt(h), 'o', markersize=4, label='medido')
    ax_raiz.plot(t, a * t + b, '-',
                 label=f'reta ajustada: $\\sqrt{{h}} = {b:.3f} {a:+.4f}\\,t$')
    ax_raiz.set_ylabel(r'$\sqrt{h}$  [mm$^{1/2}$]')
    ax_raiz.set_title('Ensaio de esvaziamento: linearizacao de Torricelli')
    ax_raiz.legend()
    ax_raiz.grid(True, alpha=0.3)

    ax_h.plot(t, h, 'o', markersize=4, label='medido')
    ax_h.plot(t, np.maximum(0.0, a * t + b) ** 2, '-', label='modelo de Torricelli')
    ax_h.set_xlabel('$t$  [s]')
    ax_h.set_ylabel('$h$  [mm]')
    ax_h.legend()
    ax_h.grid(True, alpha=0.3)

    fig.tight_layout()
    if arquivo:
        fig.savefig(arquivo, dpi=150)
        print(f'Grafico salvo em {arquivo}.')
    else:
        plt.show()


def le_argumentos():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('csv', help='CSV do ensaio de esvaziamento')

    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('-c', '--circunferencia', type=float,
                   help='circunferencia media do tanque, em mm (Tab. 2.1)')
    g.add_argument('-D', '--diametro', type=float, help='diametro do tanque, em mm')
    g.add_argument('-A', '--area', type=float,
                   help='area da secao transversal, em mm^2')

    p.add_argument('--parede', type=float, default=0.0,
                   help='espessura da parede, em mm, descontada de --circunferencia '
                        'para obter o diametro interno (padrao: 0)')
    p.add_argument('--t-min', type=float, default=None,
                   help='descarta amostras anteriores a este instante, em s')
    p.add_argument('--t-max', type=float, default=None,
                   help='descarta amostras posteriores a este instante, em s')
    p.add_argument('--h-min', type=float, default=0.0,
                   help='descarta amostras com h abaixo deste valor, em mm '
                        '(padrao: 0, que ja exclui leituras negativas)')
    p.add_argument('--figura', default=None,
                   help='salva o grafico neste arquivo em vez de abri-lo')
    p.add_argument('--sem-grafico', action='store_true', help='nao gera grafico')
    return p.parse_args()


def main():
    args = le_argumentos()
    t, h = le_csv(args.csv)
    total = len(t)

    janela = h >= args.h_min
    if args.t_min is not None:
        janela &= t >= args.t_min
    if args.t_max is not None:
        janela &= t <= args.t_max
    t, h = t[janela], h[janela]

    if len(t) < 2:
        sys.exit('ERRO: menos de duas amostras sobraram apos o recorte. '
                 'Revise --t-min/--t-max/--h-min.')

    area = area_da_secao(args)
    k, a, b, r2 = ajusta(t, h, area)

    if a >= 0:
        print('AVISO: o coeficiente angular saiu positivo, ou seja, sqrt(h) '
              'CRESCE ao longo da janela ajustada - o k abaixo sai negativo e '
              'nao tem sentido fisico. Confira se o CSV e mesmo o do ensaio de '
              'esvaziamento (Secao 2.3.2) e se --t-min/--t-max nao pegaram o '
              'trecho de enchimento.\n')

    # k sai em mm^2.5/s; 1 L = 1e6 mm^3, e 1 min = 60 s.
    k_lpm = k * 60.0 / 1.0e6

    print(f'Arquivo             : {args.csv}')
    print(f'Amostras usadas     : {len(t)} de {total} '
          f'(t de {t[0]:.1f} a {t[-1]:.1f} s, h de {h.min():.1f} a {h.max():.1f} mm)')
    print(f'Area da secao     A : {area:.1f} mm^2 = {area / 1.0e6:.6f} m^2')
    print(f'Coef. angular     a : {a:.5f} mm^0.5/s')
    print(f'Coef. linear      b : {b:.4f} mm^0.5  '
          f'(=> h(0) ajustado = {b ** 2:.2f} mm)')
    print(f'Qualidade do ajuste : R^2 = {r2:.4f}')
    print(f'Constante         k : {k:.1f} mm^2.5/s = {k_lpm:.4f} (L/min)/mm^0.5')
    print()
    print(f'Lei de Torricelli   : q_out(h) = {k_lpm:.4f} sqrt(h)   '
          '[q_out em L/min, h em mm]')
    print('Verificacao no ponto de operacao (Eq. 4): q_in0 = k sqrt(h0).')
    for h0 in (10.0, 20.0, 30.0, 40.0):
        print(f'    h0 = {h0:4.0f} mm  ->  q_in0 = {k_lpm * math.sqrt(h0):.3f} L/min')

    if not args.sem_grafico:
        desenha(t, h, a, b, args.figura)


if __name__ == '__main__':
    main()
