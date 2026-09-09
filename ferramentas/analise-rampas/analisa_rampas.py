"""Compara a rampa de enchimento e a rampa de esvaziamento do tanque do CE117.

Gera as tres figuras usadas na Aula 3 a partir dos dois ensaios de referencia
(`subida.csv`, com dreno fechado e BOMBA2 fixa, e `descida.csv`, com dreno
aberto e bomba desligada), ambos ja com a coluna `h_mm` convertida pela
calibracao nao-linear do LT:

  1. `rampas-subida-descida.pdf` -- h(t) das duas rampas.
  2. `descida-tres-modelos.pdf`  -- a rampa de descida contra tres modelos de
     dreno (reta, Torricelli puro e Torricelli com carga de offset), com o
     painel de residuos embaixo. E o residuo, nao h(t), que revela a
     curvatura.
  3. `razao-vazoes.pdf`          -- o teste da razao entre as inclinacoes das
     duas rampas medidas na MESMA altura. Como as duas leituras passam pela
     mesma curva de calibracao, a razao cancela qualquer erro estatico do LT;
     o quadrado dessa razao contra h e a evidencia limpa de que o dreno segue
     Torricelli com uma carga de offset H0 (intercepto nao nulo).

Uso:

    python3 ferramentas/analise-rampas/analisa_rampas.py \
        --dados _refs/data/nivel-h-subida-e-descida \
        --saida lab-controle-roteiro/aulas/aula-03/img

Requer numpy, scipy e matplotlib.
"""

import argparse
import csv
import pathlib

import numpy as np
from scipy.optimize import curve_fit

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Paleta categorica em ordem fixa (azul, laranja, verde-agua, amarelo) e tintas
# neutras para eixos e texto. Slots nunca sao reciclados entre as figuras.
C_SUBIDA = "#2a78d6"
C_DESCIDA = "#eb6834"
C_MODELO = "#1baf7a"
C_TERCEIRO = "#eda100"
INK = "#0b0b0b"
INK_MUTED = "#898781"
GRID = "#e1e0d9"

# Trechos descartados: a abertura da valvula no inicio da descida e o fundo do
# tanque, onde o LT perde resolucao e a secao deixa de ser prismatica.
T_MIN_DESCIDA = 2.0
H_MIN_DESCIDA = 4.0
T_MIN_SUBIDA = 3.0


def estilo():
    plt.rcParams.update(
        {
            "font.family": "serif",
            "font.size": 8,
            "axes.labelsize": 8,
            "axes.titlesize": 8,
            "legend.fontsize": 7,
            "xtick.labelsize": 7,
            "ytick.labelsize": 7,
            "axes.edgecolor": INK_MUTED,
            "axes.linewidth": 0.6,
            "xtick.color": INK_MUTED,
            "ytick.color": INK_MUTED,
            "axes.labelcolor": INK,
            "text.color": INK,
            "grid.color": GRID,
            "grid.linewidth": 0.5,
            "legend.frameon": False,
            "figure.facecolor": "white",
            "savefig.facecolor": "white",
        }
    )


def le_ensaio(caminho):
    t, h = [], []
    with open(caminho, newline="") as f:
        for linha in csv.DictReader(f):
            t.append(float(linha["t_s"]))
            h.append(float(linha["h_mm"]))
    return np.array(t), np.array(h)


def limpa_eixos(ax):
    ax.grid(True, axis="y")
    ax.set_axisbelow(True)
    for lado in ("top", "right"):
        ax.spines[lado].set_visible(False)


# --------------------------------------------------------------------------
# Modelos de esvaziamento
# --------------------------------------------------------------------------
def h_torricelli(t, h0, k):
    """Torricelli puro: A dh/dt = -k sqrt(h)."""
    return np.maximum(np.sqrt(h0) - k * t / 2.0, 0.0) ** 2


def h_torricelli_offset(t, h0, H0, k):
    """Torricelli com carga de offset: A dh/dt = -k sqrt(h + H0)."""
    return (np.sqrt(h0 + H0) - k * t / 2.0) ** 2 - H0


def ajusta_modelos(t, h):
    reta = np.polyfit(t, h, 1)
    p_tor, _ = curve_fit(h_torricelli, t, h, p0=[h[0], 0.4], maxfev=40000)
    p_off, _ = curve_fit(
        h_torricelli_offset, t, h, p0=[h[0], 200.0, 0.2], maxfev=40000
    )
    return reta, p_tor, p_off


def rms(v):
    return float(np.sqrt(np.mean(v**2)))


# --------------------------------------------------------------------------
# Figura 1 -- as duas rampas
# --------------------------------------------------------------------------
def figura_rampas(ts, hs, td, hd, saida):
    fig, ax = plt.subplots(figsize=(3.4, 2.4))
    ax.plot(ts, hs, ".", ms=2.5, color=C_SUBIDA, label="Subida (dreno fechado)")
    ax.plot(td, hd, ".", ms=2.5, color=C_DESCIDA, label="Descida (bomba desligada)")
    a_s = np.polyfit(ts[ts > T_MIN_SUBIDA], hs[ts > T_MIN_SUBIDA], 1)
    a_d = np.polyfit(td[td > T_MIN_DESCIDA], hd[td > T_MIN_DESCIDA], 1)
    ax.plot(ts, np.polyval(a_s, ts), "-", lw=1.0, color=C_SUBIDA, alpha=0.55)
    ax.plot(td, np.polyval(a_d, td), "-", lw=1.0, color=C_DESCIDA, alpha=0.55)
    ax.annotate(
        f"{a_s[0]:+.2f} mm/s",
        xy=(49, np.polyval(a_s, 49)),
        xytext=(47, 118),
        color=C_SUBIDA,
        ha="center",
        arrowprops=dict(arrowstyle="->", lw=0.6, color=C_SUBIDA),
    )
    ax.annotate(
        f"{a_d[0]:+.2f} mm/s",
        xy=(9, np.polyval(a_d, 9)),
        xytext=(19, 208),
        color=C_DESCIDA,
        ha="center",
        arrowprops=dict(arrowstyle="->", lw=0.6, color=C_DESCIDA),
    )
    ax.set_xlabel("$t$ [s]")
    ax.set_ylabel("$h$ [mm]")
    ax.set_ylim(-10, 235)
    limpa_eixos(ax)
    ax.legend(loc="lower center", ncol=1, bbox_to_anchor=(0.5, 1.0))
    fig.tight_layout()
    fig.savefig(saida / "rampas-subida-descida.pdf")
    fig.savefig(saida / "rampas-subida-descida.png", dpi=200)
    plt.close(fig)
    return a_s[0], a_d[0]


# --------------------------------------------------------------------------
# Figura 2 -- tres modelos para a descida e seus residuos
# --------------------------------------------------------------------------
def figura_modelos(td, hd, saida):
    m = (td > T_MIN_DESCIDA) & (hd > H_MIN_DESCIDA)
    t = td[m] - td[m][0]
    h = hd[m]
    reta, p_tor, p_off = ajusta_modelos(t, h)

    curvas = [
        ("Reta", np.polyval(reta, t), C_TERCEIRO),
        (r"Torricelli $\sqrt{h}$", h_torricelli(t, *p_tor), C_DESCIDA),
        (r"Torricelli $\sqrt{h+H_0}$", h_torricelli_offset(t, *p_off), C_MODELO),
    ]

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(3.4, 3.9), sharex=True, gridspec_kw={"height_ratios": [2, 1.4]}
    )
    ax1.plot(t, h, ".", ms=2.5, color=INK_MUTED, label="Medido")
    for nome, y, cor in curvas:
        ax1.plot(t, y, "-", lw=1.2, color=cor, label=nome)
    ax1.set_ylabel("$h$ [mm]")
    limpa_eixos(ax1)
    ax1.legend(loc="upper right")

    for nome, y, cor in curvas:
        r = h - y
        ax2.plot(t, r, "-", lw=1.2, color=cor)
        print(f"    {nome:28s} RMS = {rms(r):5.2f} mm   max = {np.abs(r).max():5.2f} mm")
    ax2.axhline(0.0, lw=0.6, color=INK_MUTED)
    ax2.set_xlabel("$t$ [s]")
    ax2.set_ylabel("resíduo [mm]")
    limpa_eixos(ax2)
    ax2.annotate(
        "arco do resíduo da reta:\na curvatura está aqui",
        xy=(31, -5.3),
        xytext=(6, -14.5),
        color=C_TERCEIRO,
        fontsize=6.5,
        arrowprops=dict(arrowstyle="->", lw=0.6, color=C_TERCEIRO),
    )
    fig.tight_layout()
    fig.savefig(saida / "descida-tres-modelos.pdf")
    fig.savefig(saida / "descida-tres-modelos.png", dpi=200)
    plt.close(fig)
    print(f"    H0 ajustado (direto, contaminado pelo LT) = {p_off[1]:.0f} mm")
    return p_off


# --------------------------------------------------------------------------
# Figura 3 -- teste da razao, imune ao erro estatico do LT
# --------------------------------------------------------------------------
def inclinacoes_por_faixa(t, h, faixas):
    saida = []
    for lo, hi in faixas:
        m = (h >= lo) & (h < hi)
        saida.append(np.polyfit(t[m], h[m], 1)[0] if m.sum() > 4 else np.nan)
    return np.array(saida)


def figura_razao(ts, hs, td, hd, saida):
    faixas = [(20, 50), (50, 80), (80, 110), (110, 140), (140, 170), (170, 200)]
    centros = np.array([(a + b) / 2 for a, b in faixas])
    ms = ts > T_MIN_SUBIDA
    md = td > T_MIN_DESCIDA
    s_sub = inclinacoes_por_faixa(ts[ms], hs[ms], faixas)
    s_des = inclinacoes_por_faixa(td[md], hd[md], faixas)
    razao2 = (-s_des / s_sub) ** 2

    a, b = np.polyfit(centros, razao2, 1)
    H0 = b / a
    # Torricelli puro obriga a reta a passar pela origem; ajusta-se so o ganho.
    a_origem = float(np.sum(centros * razao2) / np.sum(centros**2))
    hh = np.linspace(-H0 * 1.10, 210, 200)
    hh_pos = np.linspace(0, 210, 200)

    fig, ax = plt.subplots(figsize=(3.4, 2.6))
    ax.plot(
        hh_pos,
        a_origem * hh_pos,
        "--",
        lw=1.2,
        color=C_DESCIDA,
        label="Torricelli puro (pela origem)",
    )
    ax.plot(hh, a * hh + b, "-", lw=1.2, color=C_MODELO, label="Ajuste (intercepto $>0$)")
    ax.plot(centros, razao2, "o", ms=5, color=C_SUBIDA, label="Medido")
    ax.axhline(0.0, lw=0.6, color=INK_MUTED)
    ax.axvline(0.0, lw=0.6, color=INK_MUTED)
    ax.annotate(
        f"raiz em $h=-H_0$,\ncom $H_0 \\approx {H0:.0f}$ mm",
        xy=(-H0, 0),
        xytext=(-H0 + 4, 0.62),
        fontsize=6.5,
        color=C_MODELO,
        ha="left",
        va="bottom",
        arrowprops=dict(arrowstyle="->", lw=0.6, color=C_MODELO),
    )
    ax.set_xlabel("$h$ [mm]")
    ax.set_ylabel(r"$(\dot h_{\rm desc} / \dot h_{\rm sub})^2$")
    ax.set_xlim(-H0 * 1.25, 215)
    ax.set_ylim(-0.35, 1.75)
    limpa_eixos(ax)
    ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(saida / "razao-vazoes.pdf")
    fig.savefig(saida / "razao-vazoes.png", dpi=200)
    plt.close(fig)

    r = razao2 - (a * centros + b)
    print(f"    razao^2 = {a:.5f} h + {b:.4f}   ->  H0 = {H0:.0f} mm")
    print(f"    R2 = {1 - r.var() / razao2.var():.4f}")
    return H0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dados", required=True, type=pathlib.Path)
    ap.add_argument("--saida", required=True, type=pathlib.Path)
    args = ap.parse_args()
    args.saida.mkdir(parents=True, exist_ok=True)

    estilo()
    ts, hs = le_ensaio(args.dados / "subida.csv")
    td, hd = le_ensaio(args.dados / "descida.csv")

    print("[1] rampas-subida-descida")
    a_s, a_d = figura_rampas(ts, hs, td, hd, args.saida)
    print(f"    subida {a_s:+.3f} mm/s   descida {a_d:+.3f} mm/s")
    print("[2] descida-tres-modelos")
    figura_modelos(td, hd, args.saida)
    print("[3] razao-vazoes")
    figura_razao(ts, hs, td, hd, args.saida)


if __name__ == "__main__":
    main()
