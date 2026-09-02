"""Identifica um modelo de primeira ordem com atraso (FOPDT) a partir da
escada de degraus da Aula 3.

Le os dois CSVs gerados pelo bloco "Escada de degraus" da aba "Aula 3" de
`ensaios-gui/hub_planta.py`:

  - o CSV continuo do ensaio (colunas t_s, lt_contas, h_mm, ft2_contas,
    qin_lpm, pump2_pct);
  - o CSV de equilibrios, com o mesmo nome acrescido de "_equilibrios"
    (colunas patamar, u_pct, t_inicio_s, h_eq_mm, qin_eq_lpm) - um por
    patamar, com o instante em que o comando daquele patamar foi aplicado e
    o h/qin medios dos ultimos segundos dele (equilibrio, se o patamar durou
    o bastante).

Para cada par de patamares consecutivos (um "degrau"), calcula o ganho
empirico (Eq. 5 da Aula 3) e, pelo metodo dos dois pontos (Eq. 6) e, com
`--tangente`, tambem pelo metodo da tangente, a constante de tempo e o
atraso de transporte. Imprime uma tabela no formato da Tab. 3.4.

Uso basico (identifica todos os degraus da escada):

    python3 identificacao-degrau/identifica_degrau.py escada_degraus.csv

`--tangente` acrescenta a estimativa pelo metodo da tangente:

    python3 identificacao-degrau/identifica_degrau.py escada_degraus.csv --tangente

`--compara K tau` recebe o ganho e a constante de tempo do modelo analitico
da Aula 2 (na mesma unidade do modelo empirico, ou seja, K em mm/%, obtido
de K_b*K da Eq. 8 da Aula 3) e devolve, para o degrau de identificacao
(`--degrau-ref`, padrao 1) e para todos os demais (validacao), o RMSE e o
erro maximo de cada modelo (Eq. 7), no formato da Tab. 3.5:

    python3 identificacao-degrau/identifica_degrau.py escada_degraus.csv --compara 1.85 42.0

Sem `--sem-grafico`, `--compara` tambem abre um grafico por degrau com as
duas simulacoes sobrepostas aos dados medidos; `--figura prefixo` salva um
PNG por degrau (`prefixo_1.png`, `prefixo_2.png`, ...) em vez de abrir.
"""

import argparse
import csv
import os
import sys

import numpy as np

FRACAO_T1 = 0.283
FRACAO_T2 = 0.632


def le_escada(caminho):
    """Le t_s, h_mm, pump2_pct do CSV continuo da escada de degraus."""
    t, h, pump2 = [], [], []
    with open(caminho, newline='') as arquivo:
        leitor = csv.DictReader(arquivo)
        obrigatorias = {'t_s', 'h_mm', 'pump2_pct'}
        if leitor.fieldnames is None or not obrigatorias.issubset(leitor.fieldnames):
            sys.exit(f'ERRO: {caminho} nao tem as colunas {sorted(obrigatorias)}. '
                     'Ele foi gerado pelo bloco "Escada de degraus" da aba '
                     '"Aula 3" de ensaios-gui/hub_planta.py?')
        for linha in leitor:
            t.append(float(linha['t_s']))
            h.append(float(linha['h_mm']))
            pump2.append(float(linha['pump2_pct']) if linha['pump2_pct'] else float('nan'))
    if not t:
        sys.exit(f'ERRO: {caminho} nao tem nenhuma amostra.')
    return np.array(t), np.array(h), np.array(pump2)


def le_equilibrios(caminho):
    """Le patamar, u_pct, t_inicio_s, h_eq_mm, qin_eq_lpm do CSV de equilibrios."""
    patamares = []
    with open(caminho, newline='') as arquivo:
        leitor = csv.DictReader(arquivo)
        obrigatorias = {'u_pct', 't_inicio_s', 'h_eq_mm', 'qin_eq_lpm'}
        if leitor.fieldnames is None or not obrigatorias.issubset(leitor.fieldnames):
            sys.exit(f'ERRO: {caminho} nao tem as colunas {sorted(obrigatorias)}.')
        for linha in leitor:
            patamares.append({
                'u_pct': float(linha['u_pct']),
                't_inicio_s': float(linha['t_inicio_s']),
                'h_eq_mm': float(linha['h_eq_mm']),
                'qin_eq_lpm': float(linha['qin_eq_lpm']),
            })
    if len(patamares) < 2:
        sys.exit(f'ERRO: {caminho} tem menos de dois patamares; e preciso pelo menos '
                 'um degrau (dois patamares) para identificar um modelo.')
    return patamares


def caminho_equilibrios(caminho_escada):
    base, _ext = os.path.splitext(caminho_escada)
    return base + '_equilibrios.csv'


def recorta_janela(t, h, t_ini, t_fim, h_base):
    """Amostras de [t_ini, t_fim): tempo relativo ao inicio da janela e altura
    relativa a `h_base` (o h_eq do patamar anterior, e nao o primeiro ponto da
    janela, para ficar consistente com `delta_h_inf`)."""
    mascara = (t >= t_ini) & (t < t_fim)
    return t[mascara] - t_ini, h[mascara] - h_base


def dois_pontos(t_rel, h_rel, delta_h_inf):
    """Metodo dos dois pontos (SMITH, 1972): interpola t1 (28,3 %) e t2 (63,2 %).

    Funciona tanto para degrau positivo quanto negativo: `frac` cresce de 0 a
    1 nos dois casos, porque `delta_h_inf` carrega o sinal do degrau.
    """
    if delta_h_inf == 0 or len(t_rel) < 2:
        return float('nan'), float('nan'), float('nan'), float('nan')
    frac = h_rel / delta_h_inf
    frac_mono = np.maximum.accumulate(frac)  # robusto a ruido/overshoot
    t1 = float(np.interp(FRACAO_T1, frac_mono, t_rel))
    t2 = float(np.interp(FRACAO_T2, frac_mono, t_rel))
    tau = 1.5 * (t2 - t1)
    theta = t2 - tau
    return tau, theta, t1, t2


def tangente(t_rel, h_rel, delta_h_inf):
    """Metodo da tangente (OGATA, 2010): reta tangente no ponto de inflexao."""
    if len(t_rel) < 3:
        return float('nan'), float('nan')
    dh = np.gradient(h_rel, t_rel)
    i = int(np.argmax(dh)) if delta_h_inf > 0 else int(np.argmin(dh))
    m = dh[i]
    if m == 0:
        return float('nan'), float('nan')
    t_i, h_i = t_rel[i], h_rel[i]
    theta = t_i - h_i / m
    t_fim = t_i + (delta_h_inf - h_i) / m
    return t_fim - theta, theta


def simula_fopdt(u, K, tau, theta, T):
    """Resposta de K*exp(-theta*s)/(tau*s+1) a entrada `u`, amostrada em T.

    Variaveis de desvio: u[0] e y[0] sao nulos, e o resultado e o desvio de
    nivel em relacao ao regime permanente anterior ao degrau (mesma logica
    do Cod. 3.1 da Aula 3).
    """
    a = np.exp(-T / tau) if tau > 0 else 0.0
    d = int(round(theta / T)) if T > 0 else 0
    y = np.zeros(len(u))
    for k in range(len(u) - 1):
        u_atrasado = u[k - d] if k >= d else 0.0
        y[k + 1] = a * y[k] + K * (1 - a) * u_atrasado
    return y


def metricas(medido, simulado):
    erro = medido - simulado
    rmse = float(np.sqrt(np.mean(erro ** 2)))
    erro_max = float(np.max(np.abs(erro)))
    return rmse, erro_max


def identifica_todos(t, h, patamares, usa_tangente):
    """Identifica cada degrau consecutivo; devolve uma lista de dicts."""
    resultados = []
    for i in range(len(patamares) - 1):
        ini, fim = patamares[i], patamares[i + 1]
        t_fim_janela = patamares[i + 2]['t_inicio_s'] if i + 2 < len(patamares) else t[-1] + 1.0
        t_rel, h_rel = recorta_janela(t, h, fim['t_inicio_s'], t_fim_janela, ini['h_eq_mm'])

        delta_u = fim['u_pct'] - ini['u_pct']
        delta_h_inf = fim['h_eq_mm'] - ini['h_eq_mm']
        K_emp = delta_h_inf / delta_u if delta_u else float('nan')
        tau_dp, theta_dp, t1, t2 = dois_pontos(t_rel, h_rel, delta_h_inf)

        item = {
            'indice': i, 'h_inicial': ini['h_eq_mm'], 'u_ini': ini['u_pct'],
            'u_fim': fim['u_pct'], 'delta_u': delta_u, 'delta_h_inf': delta_h_inf,
            'K_emp': K_emp, 'tau_emp': tau_dp, 'theta': theta_dp,
            't_rel': t_rel, 'h_rel': h_rel, 't_inicio_s': fim['t_inicio_s'],
        }
        if usa_tangente:
            tau_tg, theta_tg = tangente(t_rel, h_rel, delta_h_inf)
            item['tau_tangente'] = tau_tg
            item['theta_tangente'] = theta_tg
        resultados.append(item)
    return resultados


def imprime_tabela(resultados, usa_tangente):
    print(f'{"degrau":>16} {"h ini [mm]":>11} {"du [%]":>8} '
          f'{"K_emp [mm/%]":>13} {"tau_emp [s]":>12} {"theta [s]":>10}')
    for r in resultados:
        rotulo = f'{r["u_ini"]:.0f}% a {r["u_fim"]:.0f}%'
        print(f'{rotulo:>16} {r["h_inicial"]:>11.1f} {r["delta_u"]:>8.1f} '
              f'{r["K_emp"]:>13.4f} {r["tau_emp"]:>12.2f} {r["theta"]:>10.2f}')
        if usa_tangente:
            print(f'{"  (tangente)":>16} {"":>11} {"":>8} {"":>13} '
                  f'{r["tau_tangente"]:>12.2f} {r["theta_tangente"]:>10.2f}')


def compara(resultados, degrau_ref, K_ana, tau_ana, T, figura, sem_grafico):
    if not (1 <= degrau_ref <= len(resultados)):
        sys.exit(f'ERRO: --degrau-ref {degrau_ref} fora do intervalo '
                 f'(a escada tem {len(resultados)} degraus).')
    ref = resultados[degrau_ref - 1]
    K_emp, tau_emp, theta_emp = ref['K_emp'], ref['tau_emp'], ref['theta']

    print()
    print(f'Modelo empirico congelado no degrau {degrau_ref} '
          f'({ref["u_ini"]:.0f}% a {ref["u_fim"]:.0f}%): '
          f'K_emp = {K_emp:.4f} mm/%, tau_emp = {tau_emp:.2f} s, theta = {theta_emp:.2f} s')
    print(f'Modelo analitico (Aula 2): K = {K_ana:.4f} mm/%, tau = {tau_ana:.2f} s, theta = 0 s')
    print()
    print(f'{"ensaio":>16} {"RMSE analit.":>13} {"e_max analit.":>14} '
          f'{"RMSE empir.":>12} {"e_max empir.":>13}')

    for r in resultados:
        t_rel, h_rel = r['t_rel'], r['h_rel']
        if len(t_rel) < 2:
            continue
        Ts = float(np.median(np.diff(t_rel))) if len(t_rel) > 1 else T
        u_deg = np.full(len(t_rel), r['delta_u'])
        u_deg[0] = 0.0  # variavel de desvio: entrada nula na primeira amostra

        y_ana = simula_fopdt(u_deg, K_ana, tau_ana, 0.0, Ts)
        y_emp = simula_fopdt(u_deg, K_emp, tau_emp, theta_emp, Ts)

        rmse_ana, emax_ana = metricas(h_rel, y_ana)
        rmse_emp, emax_emp = metricas(h_rel, y_emp)

        papel = 'identificacao' if r is ref else 'validacao'
        rotulo = f'{papel}: {r["u_ini"]:.0f}%-{r["u_fim"]:.0f}%'
        print(f'{rotulo:>16} {rmse_ana:>13.3f} {emax_ana:>14.3f} '
              f'{rmse_emp:>12.3f} {emax_emp:>13.3f}')

        if not sem_grafico:
            desenha_degrau(r, y_ana, y_emp, figura)


def desenha_degrau(r, y_ana, y_emp, figura_prefixo):
    import matplotlib
    if figura_prefixo:
        matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.plot(r['t_rel'], r['h_rel'], 'o', markersize=3, label='medido')
    ax.plot(r['t_rel'], y_ana, '-', label='modelo analitico (Aula 2)')
    ax.plot(r['t_rel'], y_emp, '--', label='modelo empirico (Aula 3)')
    ax.set_xlabel('$t$ desde o degrau  [s]')
    ax.set_ylabel(r'$\delta h$  [mm]')
    ax.set_title(f'Degrau {r["u_ini"]:.0f}% -> {r["u_fim"]:.0f}%')
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    if figura_prefixo:
        arquivo = f'{figura_prefixo}_{r["indice"] + 1}.png'
        fig.savefig(arquivo, dpi=150)
        print(f'Grafico salvo em {arquivo}.')
        plt.close(fig)
    else:
        plt.show()


def le_argumentos():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('csv', help='CSV continuo da escada de degraus (t_s, h_mm, pump2_pct, ...)')
    p.add_argument('--equilibrios', default=None,
                   help='CSV de equilibrios (padrao: <csv sem extensao>_equilibrios.csv)')
    p.add_argument('--tangente', action='store_true',
                   help='acrescenta a estimativa de tau e theta pelo metodo da tangente')
    p.add_argument('--compara', nargs=2, type=float, metavar=('K', 'TAU'),
                   help='K [mm/%%] e tau [s] do modelo analitico da Aula 2, para comparar '
                        'com o modelo empirico em todos os degraus (Tab. 3.5)')
    p.add_argument('--degrau-ref', type=int, default=1,
                   help='indice (1 = primeiro) do degrau usado como identificacao em '
                        '--compara; os demais entram como validacao (padrao: 1)')
    p.add_argument('--figura', default=None,
                   help='com --compara, salva um PNG por degrau com este prefixo em vez '
                        'de abrir os graficos')
    p.add_argument('--sem-grafico', action='store_true',
                   help='com --compara, nao gera graficos')
    return p.parse_args()


def main():
    args = le_argumentos()
    t, h, _pump2 = le_escada(args.csv)
    caminho_eq = args.equilibrios or caminho_equilibrios(args.csv)
    patamares = le_equilibrios(caminho_eq)

    print(f'Escada             : {args.csv}')
    print(f'Equilibrios        : {caminho_eq}')
    print(f'Patamares          : {len(patamares)}  (=> {len(patamares) - 1} degraus)')
    print()

    resultados = identifica_todos(t, h, patamares, args.tangente)
    imprime_tabela(resultados, args.tangente)

    if args.compara is not None:
        K_ana, tau_ana = args.compara
        compara(resultados, args.degrau_ref, K_ana, tau_ana, T=1.0,
               figura=args.figura, sem_grafico=args.sem_grafico)


if __name__ == '__main__':
    main()
