# clp-ab-comms

Comunicacao com o CLP Allen-Bradley da planta **TQ CE117** (IP `200.200.200.25`)
via EtherNet/IP, usando [`pycomm3`](https://github.com/ottowayi/pycomm3).

O repositorio e organizado em tres niveis, do mais simples ao mais integrado:

| pasta | o que tem |
|---|---|
| [`1_interacao-basica/`](1_interacao-basica) | scripts de linha de comando: le as tags e atua nos DACs |
| [`2_gui/`](2_gui) | interface grafica em tkinter para operar a planta e ver o nivel em tempo real |
| [`3_interacao-matlab/`](3_interacao-matlab) | daemon TCP/JSON que expoe o CLP para o MATLAB (ver o [README da pasta](3_interacao-matlab/README.md)) |

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
> CLP. Os scripts validam a faixa antes de escrever; as saidas usam
> `0..32767` (0 a 100 %).

## Instalacao

Requer Python 3.8+.

```bash
python3 -m venv .venv
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
python3 -W ignore 1_interacao-basica/atua_e_le.py 12000 8000   # VALVE, PUMP2
python3 -W ignore 1_interacao-basica/atua_e_le.py              # pergunta na tela
```

**Interface grafica** (botoes ON/OFF para bomba e valvula, grafico do nivel):

```bash
python3 -W ignore 2_gui/gui_planta.py
python3 -W ignore 2_gui/gui_planta.py --sim   # tanque simulado, sem tocar no CLP
python3 -W ignore 2_gui/gui_planta.py --ip 200.200.200.25
```

O modo `--sim` e util para testar a interface longe do laboratorio.

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

## Rede

O CLP responde em `200.200.200.25`. A maquina precisa estar na mesma rede
(por exemplo `200.200.200.10/24`) e com o EtherNet/IP liberado no firewall.
Para checar antes de rodar qualquer script:

```bash
ping -c 3 200.200.200.25
```
