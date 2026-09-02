# lab-controle-codes

Codigos do laboratorio de controle: a planta TQ CE117 e operada por uma unica
GUI, com uma aba por aula (`ensaios-gui/`); scripts de analise pos-ensaio que
nao precisam da planta ligada ficam em `ferramentas/`; e as conversoes entre
contas do CLP e unidades de engenharia ficam numa biblioteca compartilhada
(`comum/`).

## Pastas

| pasta | assunto |
|---|---|
| [`ensaios-gui/`](ensaios-gui) | **hub de ensaios**: uma unica GUI em tkinter, com uma aba por aula, para nao precisar alternar entre varios scripts de linha de comando. Mantem a unica conexao com o CLP, mostra um grafico ao vivo de `LT`/`FT2`/`PUMP2`/`VALVE` e da os sliders de comando manual |
| [`ferramentas/`](ferramentas) | scripts de apoio que trabalham sobre os CSVs gravados pelo hub (calibracao de `LT`, ajuste da constante de Torricelli, identificacao de um modelo de primeira ordem com atraso a partir da escada de degraus) ou que dao um caminho alternativo de acesso ao CLP (ponte para o MATLAB) |
| [`comum/`](comum) | biblioteca compartilhada: conversoes contas <-> volts <-> unidades de engenharia, inclusive a calibracao (nao linear) de `LT` |

## Como usar

```bash
git clone git@github.com:profilgusto/lab-controle-codes.git
cd lab-controle-codes/ensaios-gui
python3 -W ignore hub_planta.py --sim   # sem CLP, tanque simulado
```

Comece pelo hub (`ensaios-gui/`, ver o [README de la](ensaios-gui/README.md))
- e ele quem tem as ferramentas de ensaio de cada aula, organizadas em abas.
As ferramentas de `ferramentas/` (calibracao de `LT`, ajuste de Torricelli,
identificacao de degrau, ponte para o MATLAB) sao independentes entre si e do
hub; use-as quando precisar.

A calibracao (nao linear) de `LT` em `comum/conversoes.py` (levantada na
Aula 1, com a aba "Aula 1" do hub ou com `ferramentas/calibracao-lt/
calibra_lt.py`) precisa estar preenchida com os coeficientes da sua bancada
antes de qualquer ensaio que dependa de `h` em mm.

Os codigos em Python foram escritos para Python 3.13.4 (o `pyenv` abaixo cuida
da instalacao dessa versao).

## Instalando o Python com pyenv

O [pyenv](https://github.com/pyenv/pyenv) permite instalar varias versoes do
Python sem mexer na do sistema. Siga o guia oficial do seu sistema:

| sistema | guia de instalacao |
|---|---|
| Linux | [pyenv - Linux / Unix](https://github.com/pyenv/pyenv#linuxunix) |
| macOS | [pyenv - macOS (Homebrew)](https://github.com/pyenv/pyenv#homebrew-in-macos) |
| Windows | [pyenv-win - Installation](https://github.com/pyenv-win/pyenv-win/blob/master/docs/installation.md) |

No Linux e no macOS, instale antes as bibliotecas de compilacao listadas em
[Suggested build environment](https://github.com/pyenv/pyenv/wiki#suggested-build-environment)
- e delas que sai o `tkinter`, usado pelo hub. Nao esqueca de
[configurar o shell](https://github.com/pyenv/pyenv#b-set-up-your-shell-environment-for-pyenv)
e reabrir o terminal ao final.

Com o pyenv instalado, instale o Python 3.13.4 e defina-o como global:

```bash
pyenv install 3.13.4
pyenv global 3.13.4
```

Confira:

```bash
pyenv version      # deve mostrar 3.13.4
python --version   # Python 3.13.4
```

## Instalando as dependencias

Cada pasta traz seu proprio `requirements.txt`. Crie um ambiente virtual
dentro da pasta e instale ali:

```bash
cd ensaios-gui
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate

pip install --upgrade pip
pip install -r requirements.txt
```

Tudo o que fala com o CLP depende so de `pycomm3`; o hub e a calibracao de
`LT` pedem tambem **tkinter** e `numpy` respectivamente (`tkinter` vem do
sistema, nao do pip), e o ajuste de Torricelli usa `numpy` e `matplotlib`.

Para sair do ambiente virtual, `deactivate`. Se voce mesmo acrescentar
bibliotecas, registre-as com:

```bash
pip freeze > requirements.txt
```

## Organizacao

```
lab-controle-codes/
├── README.md                    <- este arquivo
├── comum/
│   └── conversoes.py            <- modulo compartilhado por todo o codigo
├── ensaios-gui/
│   ├── README.md
│   └── hub_planta.py            <- GUI unica, uma aba de ensaios por aula
└── ferramentas/
    ├── README.md
    ├── calibracao-lt/           <- calibra_lt.py (ajuste nao linear de LT)
    ├── ajuste-torricelli/       <- ajusta_torricelli.py
    ├── identificacao-degrau/    <- identifica_degrau.py (FOPDT da escada de degraus)
    └── interacao-matlab/        <- daemon_clp.py, exemplo_clp.m (+ README proprio)
```

## A biblioteca compartilhada `comum/`

`comum/conversoes.py` concentra as conversoes entre as contas do cartao AD/DA
do CLP e as grandezas de engenharia da planta (tensao, nivel `h` em mm, vazao
`q_in` em L/min), inclusive a **curva de calibracao (nao linear) de `LT`**
levantada na Aula 1. Ele nao depende de nada fora da biblioteca padrao (a
avaliacao do polinomio de calibracao e feita a mao, sem numpy), e e importado
por `ensaios-gui/hub_planta.py` - por isso a calibracao se corrige num lugar
so.

Para ver a tabela de conversao:

```bash
python3 comum/conversoes.py
```

O hub acrescenta `comum/` ao `sys.path` sozinho, a partir do proprio caminho
do arquivo; nao e preciso `PYTHONPATH` nem instalar nada:

```python
import os, sys
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RAIZ, 'comum'))

from conversoes import contas_para_altura
```

## Adicionando uma aula nova

As ferramentas de ensaio de uma aula nova entram como uma aba do hub, nao
como uma pasta `aula-NN/` separada: em `ensaios-gui/hub_planta.py`, troque o
placeholder `AbaEmDesenvolvimento` daquela aula por uma classe `AbaAulaN`
propria, seguindo o padrao de `AbaAula1`/`AbaAula2`/`AbaAula3` no mesmo arquivo (uma
aba por aula, comunicando com a planta so atraves de `Janela.aplica_comando`
e do fluxo de amostras de `Aquisicao`). Scripts de analise pos-ensaio que a
aula precisar (sem depender da planta ligada) entram como uma pasta nova em
`ferramentas/`.
