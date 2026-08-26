# lab-controle-codes

Codigos das aulas do laboratorio de controle. Cada aula tem sua propria pasta,
com os scripts e um `README.md` explicando o que roda e como rodar.

## Aulas

| pasta | assunto |
|---|---|
| [`comum/`](comum) | biblioteca compartilhada por todas as aulas (conversoes contas <-> volts <-> unidades de engenharia) |
| [`aula-01/`](aula-01) | comunicacao com o CLP Allen-Bradley da planta TQ CE117 via EtherNet/IP: leitura de tags, atuacao nos DACs, GUI em tkinter e ponte para o MATLAB |
| [`aula-02/`](aula-02) | modelagem analitica do tanque de processo: registro de ensaios com marcacao de tempo e estimacao da constante da lei de Torricelli |

## Como usar

Clone o repositorio e entre na pasta da aula desejada; o `README.md` de la tem
os requisitos, a instalacao e os comandos:

```bash
git clone git@github.com:profilgusto/lab-controle-codes.git
cd lab-controle-codes/aula-01
```

As aulas sao independentes entre si: da para comecar por qualquer uma, desde
que a calibracao de `LT` em `comum/conversoes.py` (levantada na Aula 1) esteja
preenchida com os coeficientes da sua bancada.

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
- e delas que sai o `tkinter`, usado pela GUI da aula 01. Nao esqueca de
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
dentro da pasta da aula e instale ali:

```bash
cd aula-01
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate

pip install --upgrade pip
pip install -r 1_interacao-basica/requirements.txt
```

Se a aula tiver mais de uma pasta de codigo, os arquivos podem ser instalados
de uma vez (na aula 02, por exemplo,
`pip install -r 1_ensaio/requirements.txt -r 2_ajuste/requirements.txt`). Tudo
o que fala com o CLP depende so de `pycomm3`; a GUI da aula 01 pede tambem o
**tkinter**, que vem do sistema e nao do pip, e o ajuste da aula 02 usa `numpy`
e `matplotlib`.

Para sair do ambiente virtual, `deactivate`. Se voce mesmo acrescentar
bibliotecas, registre-as com:

```bash
pip freeze > requirements.txt
```

## Organizacao

```
lab-controle-codes/
├── README.md                 <- este arquivo
├── comum/
│   └── conversoes.py         <- modulo compartilhado por todas as aulas
├── aula-01/
│   ├── README.md             <- instrucoes especificas da aula
│   ├── 1_interacao-basica/   <- ler_tags.py, atua_e_le.py
│   ├── 2_gui/                <- gui_planta.py (tkinter)
│   └── 3_interacao-matlab/   <- daemon_clp.py, exemplo_clp.m (+ README proprio)
├── aula-02/
│   ├── README.md
│   ├── 1_ensaio/             <- registra_ensaio.py
│   └── 2_ajuste/             <- ajusta_torricelli.py
└── aula-NN/                  <- proximas aulas, mesma estrutura
```

Cada pasta de codigo traz seu proprio `requirements.txt`; pastas com material
mais extenso (como `aula-01/3_interacao-matlab/`) tem tambem um `README.md`
proprio, ligado a partir do README da aula.

Ao adicionar uma aula nova, crie a pasta `aula-NN/` com seu proprio
`README.md` e acrescente uma linha na tabela de aulas acima.

## A biblioteca compartilhada `comum/`

`comum/conversoes.py` concentra as conversoes entre as contas do cartao AD/DA
do CLP e as grandezas de engenharia da planta (tensao, nivel `h` em mm, vazao
`q_in` em L/min), inclusive a **curva de calibracao de `LT`** levantada na
Aula 1. Ele nao depende de nada fora da biblioteca padrao, e e importado pelos
codigos de todas as aulas - por isso a calibracao se corrige num lugar so.

Para ver a tabela de conversao:

```bash
python3 comum/conversoes.py
```

Os scripts acrescentam `comum/` ao `sys.path` sozinhos, a partir do proprio
caminho do arquivo; nao e preciso `PYTHONPATH` nem instalar nada. Um script
novo, em `aula-NN/<pasta>/`, so precisa repetir o cabecalho:

```python
import os, sys
RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(RAIZ, 'comum'))

from conversoes import contas_para_altura
```
