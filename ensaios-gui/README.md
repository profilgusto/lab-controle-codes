# ensaios-gui - hub de ensaios da planta

Uma unica GUI que reune, numa so janela, as ferramentas de ensaio de todas as
aulas. Existe para resolver o atrito de ter um script de linha de comando por
tarefa (ler tags aqui, atuar acola, gravar um ensaio noutro terminal): o hub
mantem uma UNICA conexao com o CLP (numa thread de aquisicao) e organiza as
ferramentas de cada aula em abas.

```bash
python3 -W ignore hub_planta.py
python3 -W ignore hub_planta.py --sim         # tanque simulado, sem tocar no CLP
python3 -W ignore hub_planta.py --ip 200.200.200.25
python3 -W ignore hub_planta.py --janela 300  # janela inicial do grafico, em segundos
```

## O que a janela mostra sempre, independente da aba

- **Comando manual**: um slider (0-100 %) e um botao ON/OFF para `VALVE` (a
  valvula `S`) e outro par igual para `PUMP2`, com o mesmo intertravamento de
  seguranca da Aula 1 - `PUMP2` so e liberado (slider e botao) com `VALVE` em
  **100 %**; baixar `VALVE` de 100 % desliga `PUMP2` junto. Os botoes e os
  sliders de cada atuador ficam sempre em sincronia: mover o slider atualiza
  o botao, e vice-versa.
- **Grafico ao vivo**: `LT`, `FT2`, `PUMP2` e `VALVE`, todos convertidos para
  `%` do fundo de escala do instrumento (`conta_para_percentual`, de
  `comum/conversoes.py`) - por isso cabem no mesmo eixo, apesar de serem
  grandezas diferentes (nivel, vazao, dois comandos). A largura da janela de
  tempo se escolhe no seletor (30 s a 30 min).
- **Barra de status**: mostra a leitura corrente (quando nenhum ensaio
  automatico esta em curso) ou o nome do ensaio que tomou o controle da
  planta.

## Abas

| aba | o que tem |
|---|---|
| **Aula 1** | leitura de todas as tags (`PUMP2`, `VALVE`, `FT2`, `PT`, `TT5`, `LT`, em contas/volts/unidade de engenharia); assistente de calibracao de `LT` (Secao 1.3.4) - adiciona pontos com a leitura atual de `LT` e o `h` medido na regua, salva CSV no formato de `ferramentas/calibracao-lt/calibra_lt.py` e ja mostra RMSE/erro maximo dos graus 1-3 e os coeficientes prontos para colar em `LT_COEFS_H_DE_CONTAS` |
| **Aula 2** | ensaio de esvaziamento (Secao 2.3.2): grava um CSV a partir do instante em que a gravacao comeca, sem atuar em nada; ensaio de degrau (Secao 2.3.3): aplica a condicao inicial, toma o controle de `VALVE`/`PUMP2` (os sliders ficam bloqueados), aplica o degrau no instante programado e devolve o controle ao final. Os dois geram um CSV com as colunas `t_s, lt_contas, h_mm, ft2_contas, qin_lpm, pump2_pct` |
| **Aula 3** | varredura estatica do atuador (Secao 3.3.1): percorre uma lista de comandos de `PUMP2` (crescente e depois decrescente) e grava a media de `FT2` de cada patamar; escada de degraus (Secao 3.3.2): aplica uma sequencia configuravel de comandos, cada um mantido pela mesma duracao, gravando um CSV continuo (mesmas colunas da Aula 2) e, com o sufixo `_equilibrios`, um segundo CSV com uma linha por patamar (`patamar, u_pct, t_inicio_s, h_eq_mm, qin_eq_lpm`) - consumido por `ferramentas/identificacao-degrau/identifica_degrau.py` |
| **Aula 4-7** | placeholders ("material ainda em desenvolvimento"), no mesmo estado do roteiro (ver `CLAUDE.md` na raiz do repositorio). Ao escrever o roteiro de uma aula nova, acrescente a aba correspondente aqui seguindo o padrao de `AbaAula1`/`AbaAula2`/`AbaAula3` em `hub_planta.py` |

Ferramentas de **analise pos-ensaio** que nao precisam da planta ligada
continuam como scripts separados, em [`../ferramentas`](../ferramentas), sem
equivalente no hub: `ferramentas/calibracao-lt/calibra_lt.py` (se voce
preferir ajustar fora da GUI), `ferramentas/ajuste-torricelli/
ajusta_torricelli.py` (ajuste de `k` a partir do CSV do esvaziamento) e
`ferramentas/identificacao-degrau/identifica_degrau.py` (identifica um
modelo de primeira ordem com atraso a partir da escada de degraus da Aula 3).

## Por que uma unica conexao

`hub_planta.py` tem uma unica thread (`Aquisicao`) dona da sessao com o CLP:
ela le todas as tags em ciclo e escreve o ultimo comando pedido. Sliders,
botoes ON/OFF e um ensaio automatico (ex.: o degrau) passam todos pelo mesmo
ponto de escrita (`Janela.aplica_comando`), que reaplica o intertravamento
(`PUMP2` so aceito com `VALVE` em 100 %) mesmo quando quem esta comandando e
um ensaio, nao o usuario. Enquanto um ensaio tem o controle, os sliders e os
botoes ficam desabilitados; ao terminar (por duracao ou por "parar"), o
controle volta aos sliders/botoes, no ultimo valor aplicado - sem zerar as
saidas.

Cada aba que grava CSV (calibracao, esvaziamento, degrau) consome o mesmo
fluxo de amostras dessa thread unica - nao abre conexao propria.

## Instalacao

Requer Python 3.13.4 (ver o
[README da raiz](../README.md#instalando-o-python-com-pyenv)) e depende de
`pycomm3` e `numpy` (este ultimo so para a calibracao de `LT`, na Aba 1):

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

O **tkinter** e pacote do sistema, nao vem do pip:

```bash
sudo apt install python3-tk       # Ubuntu/Debian
sudo dnf install python3-tkinter  # Fedora
```

`--sim` substitui o CLP por um tanque de Torricelli simulado, calibrado para
reproduzir os numeros do ensaio piloto do roteiro (equilibrio em ~180 mm com
`PUMP2` e `S` em 100 %, esvaziamento de 180 mm em ~50 s), util para testar a
interface longe do laboratorio.
