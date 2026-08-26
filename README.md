# lab-controle-codes

Codigos das aulas do laboratorio de controle. Cada aula tem sua propria pasta,
com os scripts e um `README.md` explicando o que roda e como rodar.

## Aulas

| pasta | assunto |
|---|---|
| [`aula-01/`](aula-01) | comunicacao com o CLP Allen-Bradley da planta TQ CE117 via EtherNet/IP: leitura de tags, atuacao nos DACs, GUI em tkinter e ponte para o MATLAB |

## Como usar

Clone o repositorio e entre na pasta da aula desejada; o `README.md` de la tem
os requisitos, a instalacao e os comandos:

```bash
git clone git@github.com:profilgusto/lab-controle-codes.git
cd lab-controle-codes/aula-01
```

Os codigos em Python foram escritos para Python 3.13.4 (o `pyenv` abaixo cuida
da instalacao dessa versao).

## Instalando o Python com pyenv

O [pyenv](https://github.com/pyenv/pyenv) permite instalar varias versoes do
Python sem mexer na do sistema.

**1. Instalar o pyenv**

```bash
# macOS (Homebrew)
brew install pyenv

# Linux (Ubuntu/Debian) - dependencias de compilacao + pyenv
sudo apt update && sudo apt install -y build-essential libssl-dev zlib1g-dev \
  libbz2-dev libreadline-dev libsqlite3-dev curl git libncursesw5-dev \
  xz-utils tk-dev libxml2-dev libxmlsec1-dev libffi-dev liblzma-dev
curl -fsSL https://pyenv.run | bash
```

**2. Configurar o shell** (uma vez; troque `~/.zshrc` por `~/.bashrc` se usar
bash) e reabrir o terminal:

```bash
echo 'export PYENV_ROOT="$HOME/.pyenv"' >> ~/.zshrc
echo '[[ -d $PYENV_ROOT/bin ]] && export PATH="$PYENV_ROOT/bin:$PATH"' >> ~/.zshrc
echo 'eval "$(pyenv init - zsh)"' >> ~/.zshrc
exec "$SHELL"
```

**3. Instalar o Python 3.13.4 e defini-lo como global**

```bash
pyenv install 3.13.4
pyenv global 3.13.4
```

**4. Conferir**

```bash
pyenv version      # deve mostrar 3.13.4 (set by ~/.pyenv/version)
python --version   # Python 3.13.4
```

O `tkinter` (usado pela GUI da aula 01) e compilado junto com o Python. No
Linux, instale o `tk-dev` **antes** do `pyenv install`; se esquecer, basta
instalar o pacote e rodar `pyenv install 3.13.4` de novo.

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

Para sair do ambiente virtual, `deactivate`. Se voce mesmo acrescentar
bibliotecas, registre-as com:

```bash
pip freeze > requirements.txt
```

## Organizacao

```
lab-controle-codes/
├── README.md      <- este arquivo
├── aula-01/
│   ├── README.md  <- instrucoes especificas da aula
│   └── ...        <- codigos da aula
└── aula-NN/       <- proximas aulas, mesma estrutura
```

Ao adicionar uma aula nova, crie a pasta `aula-NN/` com seu proprio
`README.md` e acrescente uma linha na tabela de aulas acima.
