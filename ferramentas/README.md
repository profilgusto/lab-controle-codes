# ferramentas

Scripts de apoio que **nao** entraram no hub (`ensaios-gui/hub_planta.py`)
porque nao sao ensaios ao vivo na planta - sao analise pos-ensaio ou uma
ponte para outro ambiente. Cada um roda sozinho, com seu proprio
`requirements.txt`.

| pasta | o que faz | onde aparece no roteiro |
|---|---|---|
| [`calibracao-lt/`](calibracao-lt) | `calibra_lt.py`: ajusta a curva (nao linear) de calibracao de LT a um CSV de pontos `h_mm, contas` e imprime os coeficientes para `comum/conversoes.py`. Alternativa de linha de comando ao assistente ja embutido na aba "Aula 1" do hub | Secao 1.3.4 |
| [`ajuste-torricelli/`](ajuste-torricelli) | `ajusta_torricelli.py`: le o CSV do ensaio de esvaziamento (gerado pela aba "Aula 2" do hub) e estima a constante `k` da lei de Torricelli por minimos quadrados | Secao 2.3.2 |
| [`identificacao-degrau/`](identificacao-degrau) | `identifica_degrau.py`: le os CSVs da escada de degraus (gerados pela aba "Aula 3" do hub, o continuo e o de equilibrios) e identifica um modelo de primeira ordem com atraso (ganho, constante de tempo e atraso) pelo metodo dos dois pontos e, opcionalmente, pelo metodo da tangente; `--compara` avalia esse modelo contra o modelo analitico da Aula 2 (RMSE e erro maximo, identificacao e validacao) | Secoes 3.3.1-3.3.2 |
| [`interacao-matlab/`](interacao-matlab) | `daemon_clp.py` + `exemplo_clp.m`: ponte TCP/JSON que expoe o CLP para quem preferir programar em MATLAB em vez de usar o hub | material extra |

## Relacao com `ensaios-gui/`

O [hub de ensaios](../ensaios-gui) concentra a **aquisicao e a atuacao** na
planta (ler tags, mover VALVE/PUMP2, gravar os CSVs de cada ensaio). As
ferramentas desta pasta trabalham **depois**, sobre os CSVs ja gravados (ou,
no caso do MATLAB, como um caminho alternativo de acesso ao CLP para quem nao
vai usar o hub). Nenhuma delas depende de `comum/conversoes.py` via
`sys.path`; cada uma e autocontida (usa so `numpy`/`csv`/`pycomm3` conforme o
caso).

## Instalacao

Requer Python 3.13.4 (ver o
[README da raiz](../README.md#instalando-o-python-com-pyenv)). Cada pasta tem
seu proprio `requirements.txt`:

```bash
cd ferramentas/calibracao-lt
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```
