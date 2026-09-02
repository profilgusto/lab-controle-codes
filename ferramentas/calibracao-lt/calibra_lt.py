"""Ajusta a curva de calibracao do transmissor de nivel LT (Secao 1.3.4).

LT e classificado como NAO LINEAR pelo fabricante (Tab. 1, p. 16 do manual da
CE117) - ao contrario de TT, FT e PT, que sao lineares. Uma reta ajustada aos
pontos da Tab. 1.6 erra vários milimetros; este programa ajusta polinomios de
graus 1, 2 e 3 aos pontos medidos (h em mm x contas de LT_ADC), imprime o RMSE,
o erro maximo e o R^2 de cada grau - reproduzindo a comparacao que motivou o
uso de um polinomio em vez de uma reta em `conversoes.py` -, escreve a equacao
de cada polinomio ajustado de forma legivel e devolve os coeficientes do grau
escolhido, prontos para colar em `LT_COEFS_H_DE_CONTAS`.

Uso, a partir de um CSV com colunas `h_mm` e `contas` (a Tab. 1.6 preenchida
e digitada, ou exportada pela aba "Aula 1" de `ensaios-gui/hub_planta.py`):

    python3 calibracao-lt/calibra_lt.py calibracao_lt.csv

Sem `--sem-grafico`, abre o grafico com os pontos medidos e as tres curvas
sobrepostas; `--figura arquivo.png` salva em vez de abrir. `--grau` escolhe
qual grau usar no coeficiente impresso ao final (padrao: 3).
"""

import argparse
import csv
import sys

import numpy as np


def le_csv(caminho):
    """Le as colunas h_mm e contas da Tab. 1.6 digitada em CSV."""
    h, contas = [], []
    with open(caminho, newline='') as arquivo:
        leitor = csv.DictReader(arquivo)
        if leitor.fieldnames is None or 'h_mm' not in leitor.fieldnames \
                or 'contas' not in leitor.fieldnames:
            sys.exit(f'ERRO: {caminho} nao tem as colunas h_mm e contas. '
                     'E a Tab. 1.6 (calibracao de LT) digitada em CSV?')
        for linha in leitor:
            h.append(float(linha['h_mm']))
            contas.append(float(linha['contas']))
    if len(h) < 4:
        sys.exit(f'ERRO: {caminho} tem menos de 4 pontos; um ajuste cubico '
                 'precisa de pelo menos 4 para nao ficar sobreajustado.')
    return np.array(h), np.array(contas)


def ajusta_grau(h, contas, grau):
    """Ajusta h = p(contas) de um dado grau; devolve (coefs, rmse, erro_max, r2)."""
    coefs = np.polyfit(contas, h, grau)
    h_pred = np.polyval(coefs, contas)
    residuos = h_pred - h
    rmse = np.sqrt(np.mean(residuos ** 2))
    erro_max = np.max(np.abs(residuos))
    ss_res = np.sum(residuos ** 2)
    ss_tot = np.sum((h - h.mean()) ** 2)
    r2 = 1.0 - ss_res / ss_tot
    return coefs, rmse, erro_max, r2


def formata_polinomio(coefs, variavel='N'):
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
            corpo = f'{abs(a):.4e}*{variavel}'
        else:
            corpo = f'{abs(a):.4e}*{variavel}^{expoente}'
        termos.append((sinal, corpo))

    primeiro_sinal, primeiro_corpo = termos[0]
    expressao = ('-' if primeiro_sinal == '-' else '') + primeiro_corpo
    for sinal, corpo in termos[1:]:
        expressao += f' {sinal} {corpo}'
    return f'h({variavel}) = {expressao}  [mm]'


def desenha(h, contas, ajustes, arquivo):
    import matplotlib
    if arquivo:
        matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 5))
    ax.plot(contas, h, 'o', markersize=5, label='pontos medidos (Tab. 1.6)')

    c_fino = np.linspace(contas.min(), contas.max(), 300)
    for grau, (coefs, rmse, _erro_max, r2) in ajustes.items():
        ax.plot(c_fino, np.polyval(coefs, c_fino), '-',
                label=f'grau {grau} (RMSE = {rmse:.2f} mm, R² = {r2:.4f})')

    ax.set_xlabel('LT\\_ADC  [contas]')
    ax.set_ylabel('$h$  [mm]')
    ax.set_title('Calibracao de LT: reta vs. polinomio')
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    if arquivo:
        fig.savefig(arquivo, dpi=150)
        print(f'Grafico salvo em {arquivo}.')
    else:
        plt.show()


def le_argumentos():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('csv', help='CSV da Tab. 1.6 (colunas h_mm, contas)')
    p.add_argument('--grau', type=int, default=3, choices=(1, 2, 3),
                   help='grau do polinomio a destacar no coeficiente final (padrao: 3)')
    p.add_argument('--figura', default=None,
                   help='salva o grafico neste arquivo em vez de abri-lo')
    p.add_argument('--sem-grafico', action='store_true', help='nao gera grafico')
    return p.parse_args()


def main():
    args = le_argumentos()
    h, contas = le_csv(args.csv)

    print(f'Arquivo         : {args.csv}')
    print(f'Pontos medidos  : {len(h)} (h de {h.min():.0f} a {h.max():.0f} mm)')
    print()
    print(f'{"grau":>4}   {"RMSE [mm]":>10}   {"erro max [mm]":>14}   {"R²":>8}')

    ajustes = {}
    for grau in (1, 2, 3):
        coefs, rmse, erro_max, r2 = ajusta_grau(h, contas, grau)
        ajustes[grau] = (coefs, rmse, erro_max, r2)
        print(f'{grau:>4}   {rmse:>10.2f}   {erro_max:>14.2f}   {r2:>8.5f}')

    print()
    print('Equacoes ajustadas (h em mm, N = leitura de LT em contas):')
    for grau in (1, 2, 3):
        coefs = ajustes[grau][0]
        print(f'  grau {grau}: {formata_polinomio(coefs)}')

    coefs, rmse, erro_max, r2 = ajustes[args.grau]
    print()
    print(f'Coeficientes do grau {args.grau} (do maior para o menor, para colar em '
          f'LT_COEFS_H_DE_CONTAS em conversoes.py):')
    print(repr(tuple(coefs.tolist())))

    if not args.sem_grafico:
        desenha(h, contas, ajustes, args.figura)


if __name__ == '__main__':
    main()
