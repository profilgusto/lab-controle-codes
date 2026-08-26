# Aula 01 - Comunicacao com o CLP

Comunicacao com o CLP Allen-Bradley da planta **TQ CE117** (IP `200.200.200.25`)
via EtherNet/IP, usando [`pycomm3`](https://github.com/ottowayi/pycomm3).

Os caminhos e comandos desta pagina sao relativos a esta pasta (`aula-01/`).

O material da aula e organizado em tres niveis, do mais simples ao mais integrado:

| pasta | o que tem | onde aparece no roteiro |
|---|---|---|
| [`1_interacao-basica/`](1_interacao-basica) | scripts de linha de comando: le as tags e atua nos DACs | Secoes 1.3.1 e 1.3.2 |
| [`2_gui/`](2_gui) | interface grafica em tkinter para operar a planta e ver o nivel em tempo real | Secao 1.3.4 |
| [`3_interacao-matlab/`](3_interacao-matlab) | daemon TCP/JSON que expoe o CLP para o MATLAB (ver o [README da pasta](3_interacao-matlab/README.md)) | material extra |

## A planta

Tags do `MainProgram`, todas `INT`:

| tag | sentido | o que e |
|---|---|---|
| `PUMP2_DAC` | saida | acionamento da bomba 2 |
| `VALVE_DAC` | saida | abertura da valvula |
| `FT2_ADC` | entrada | vazao |
| `PT_ADC` | entrada | pressao |
| `TT5_ADC` | entrada | temperatura |
| `LT_ADC` | entrada | nivel do tanque |

O cartao AD/DA trabalha em contas: `-32768..32767` contas correspondem a
`-10,5..+10,5 V`, ou seja `volts = contas * 10.5 / 32768`.

> **Cuidado com a faixa do INT.** `32768` nao cabe num `INT` e vira `-32768` no
> CLP. Os scripts validam a faixa antes de escrever.

> **O fundo de escala do cartao nao e o do instrumento.** Os instrumentos vao
> de 0 a 10 V, o cartao vai a 10,5 V: 100 % de comando sao **31207** contas, e
> nao 32767. Um sensor saturado tambem indica 31207.

## Seguranca: S antes de PUMP2

A valvula proporcional `S` tem de estar aberta **antes** de qualquer
acionamento de `PUMP2`: a bomba contra a valvula fechada pressuriza a linha e
pode danificar a planta. O CLP nao implementa essa trava - quem implementa sao
os scripts daqui:

- `atua_e_le.py` recusa a escrita se `PUMP2 > 0` com `VALVE = 0`;
- a GUI nao deixa ligar `PUMP2` com `S` fechada, e desliga a bomba junto se
  `S` for fechada;
- as duas escrevem sempre `VALVE_DAC` antes de `PUMP2_DAC`.

## O modulo `conversoes.py`

Todas as conversoes acima (contas <-> volts <-> `%`, e a curva de calibracao de
`LT` levantada na pratica) ficam em
[`../comum/conversoes.py`](../comum/conversoes.py), compartilhado com as demais
aulas. Os scripts o importam sozinhos, sem `PYTHONPATH`:

```bash
python3 ../comum/conversoes.py   # tabela contas -> volts -> h -> q
```

**Ao terminar a calibracao de `LT` (Tab. 1.5 do roteiro), edite `LT_A` e `LT_B`
no topo desse arquivo.** E dessa reta que a Aula 2 tira o `h` em milimetros de
todos os ensaios.

## Instalacao

Requer Python 3.13.4 - a instalacao via `pyenv` esta no
[README da raiz](../README.md#instalando-o-python-com-pyenv).

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r 1_interacao-basica/requirements.txt
```

Cada pasta tem seu proprio `requirements.txt` (todas dependem apenas de
`pycomm3==1.2.16`). A GUI precisa tambem do **tkinter**, que nao vem do pip:

```bash
sudo apt install python3-tk      # Ubuntu/Debian
sudo dnf install python3-tkinter # Fedora
```

Os cabecalhos dos scripts mostram as chamadas com `PYTHONPATH=libs`, que serve
para o caso de o `pycomm3` estar copiado numa pasta `libs/` local (maquina de
laboratorio sem `pip`). Com o venv acima, o prefixo e dispensavel.

O `-W ignore` so silencia os avisos de depreciacao do `pycomm3`.

## Uso rapido

**Ler todas as tags:**

```bash
python3 -W ignore 1_interacao-basica/ler_tags.py
```

**Escrever nos DACs, esperar 2 s e ler de volta:**

```bash
python3 -W ignore 1_interacao-basica/atua_e_le.py --pct 100 0  # abre S em 100 %
python3 -W ignore 1_interacao-basica/atua_e_le.py --pct 100 40 # S aberta, bomba a 40 %
python3 -W ignore 1_interacao-basica/atua_e_le.py 31207 0      # o mesmo, em contas
python3 -W ignore 1_interacao-basica/atua_e_le.py              # pergunta na tela
```

O primeiro comando e o do passo 3.3 da Secao 1.3.1 e o das condicoes iniciais
da Secao 1.3.2 ("abrir em 100 % a valvula S"). Sem `--pct`, os valores sao
contas do DAC, como no Cod. 1.1 do roteiro.

**Interface grafica** (botoes ON/OFF para bomba e valvula, grafico do nivel):

```bash
python3 -W ignore 2_gui/gui_planta.py
python3 -W ignore 2_gui/gui_planta.py --sim         # tanque simulado, sem tocar no CLP
python3 -W ignore 2_gui/gui_planta.py --ip 200.200.200.25
python3 -W ignore 2_gui/gui_planta.py --janela 300  # janela inicial de 5 min
```

O modo `--sim` e util para testar a interface longe do laboratorio.

E a interface da **Secao 1.3.4**: os botoes acionam `S` e `PUMP2` (nesta
ordem, pelo intertravamento), "limpar grafico" separa a curva de subida da de
descida do nivel, e o painel de leitura mostra `LT` **em contas e em volts** -
as duas colunas que vao para a Tab. 1.5, ao lado da altura lida na regua.

O grafico e desenhado num `Canvas` do proprio tkinter (nao precisa de
matplotlib) e le a planta a cada 0,5 s numa thread separada, para a janela nao
travar enquanto espera o CLP. A largura da janela de tempo se escolhe na
propria interface entre 30 s e 30 min; `--janela` define com qual delas o
programa abre. O historico guardado em memoria vai ate a maior dessas faixas,
entao mudar para uma janela maior mostra tambem o que ja tinha passado.

Ao terminar a calibracao, ajuste `LT_A` e `LT_B` em `../comum/conversoes.py`
(ver abaixo).

**MATLAB** (num terminal, o daemon; no MATLAB, o script):

```bash
python3 -W ignore 3_interacao-matlab/daemon_clp.py
```

```matlab
>> cd 3_interacao-matlab
>> exemplo_clp
```

Os detalhes do protocolo JSON estao no
[README de `3_interacao-matlab/`](3_interacao-matlab/README.md).

## Depois desta aula

Os coeficientes `LT_A` e `LT_B` gravados em `../comum/conversoes.py` sao a
saida pratica da Aula 1: e deles que a
[Aula 2](../aula-02/README.md) tira o `h` em milimetros dos ensaios de
esvaziamento e de degrau.

## Rede

O CLP responde em `200.200.200.25`. A maquina precisa estar na mesma rede
(por exemplo `200.200.200.10/24`) e com o EtherNet/IP liberado no firewall.
Para checar antes de rodar qualquer script:

```bash
ping -c 3 200.200.200.25
```
