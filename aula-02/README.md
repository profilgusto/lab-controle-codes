# Aula 02 - Modelagem analitica do tanque de processo

Codigos de apoio dos ensaios da Aula 2: registro de series temporais na planta
**TQ CE117** e estimacao da constante `k` da lei de Torricelli.

Os caminhos e comandos desta pagina sao relativos a esta pasta (`aula-02/`).

| arquivo | o que faz |
|---|---|
| [`../comum/conversoes.py`](../comum/conversoes.py) | biblioteca compartilhada por todas as aulas: conversoes contas <-> volts <-> `h` [mm] / `q_in` [L/min]; **a curva de calibracao de LT levantada na Aula 1 e editada aqui** |
| [`1_ensaio/registra_ensaio.py`](1_ensaio/registra_ensaio.py) | laco de amostragem de periodo fixo `T`; grava `t`, `h` e `q_in` num CSV; com `--degrau`, aplica um degrau em `PUMP2` no instante programado |
| [`2_ajuste/ajusta_torricelli.py`](2_ajuste/ajusta_torricelli.py) | le o CSV do esvaziamento, ajusta a reta de `sqrt(h)` contra `t` com `numpy.polyfit` e imprime `k` |

## Antes de tudo: calibre o `conversoes.py`

O modulo mora em [`comum/`](../comum), na raiz do repositorio, e e o mesmo
usado pela Aula 1 - os scripts o encontram sozinhos, sem `PYTHONPATH`.

`contas_para_altura()` usa a reta `h = LT_A * V + LT_B` ajustada aos pontos da
Tab. 1.5 (calibracao de `LT`, Aula 1). As constantes `LT_A` e `LT_B` no topo de
`comum/conversoes.py` vem preenchidas com o comportamento **nominal** (0 V
tanque vazio, 10 V tanque cheio, ~180 mm de coluna util) - **substitua pelos
coeficientes da sua bancada**, senao todo o `h` gravado nos CSVs estara errado,
e com ele o `k`, o `R` e o `tau` do modelo.

Para conferir a tabela de conversao:

```bash
python3 ../comum/conversoes.py
```

## Instalacao

Requer Python 3.13.4 - a instalacao via `pyenv` esta no
[README da raiz](../README.md#instalando-o-python-com-pyenv).

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r 1_ensaio/requirements.txt -r 2_ajuste/requirements.txt
```

O ensaio depende so de `pycomm3`; o ajuste, de `numpy` e `matplotlib`.

## Ensaio de esvaziamento (Secao 2.3.2 do roteiro)

Encha o tanque com o dreno **fechado**, desligue `PUMP2` e `S`, e dispare o
comando abaixo **no exato instante em que abrir a valvula de dreno** - esse e
o `t = 0` do ensaio. O programa so observa, nao atua em nada:

```bash
python3 -W ignore 1_ensaio/registra_ensaio.py -T 2 -o esvaziamento.csv
```

`-T 2` e o periodo pedido no roteiro: partindo de 180 mm com o dreno todo
aberto, o esvaziamento dura cerca de 50 s, e um periodo maior renderia poucos
pontos para o ajuste.

`--ip` aponta para outro CLP, caso a bancada nao esteja no
`200.200.200.25` de sempre.

Ctrl+C encerra quando o nivel estabilizar. O CSV e gravado linha a linha, entao
continua valido mesmo se o ensaio for interrompido.

Em seguida, informe a circunferencia media medida na Secao 2.3.1 e estime `k`:

```bash
python3 2_ajuste/ajusta_torricelli.py esvaziamento.csv --circunferencia 358
```

Perto de `h = 0` o esvaziamento desacelera e `LT` perde resolucao; se a cauda
do ensaio estragar o ajuste, recorte-a:

```bash
python3 2_ajuste/ajusta_torricelli.py esvaziamento.csv -c 358 --t-max 55
python3 2_ajuste/ajusta_torricelli.py esvaziamento.csv -c 358 --h-min 5
```

Alem de `-c/--circunferencia`, a area pode vir de `-D/--diametro` ou
`-A/--area`; com `--parede`, a espessura da parede e descontada da
circunferencia (medida por fora) para dar o diametro interno. A janela do
ajuste se recorta com `--t-min`, `--t-max` e `--h-min`.

Use `--figura ajuste.png` para salvar o grafico em vez de abri-lo, e
`--sem-grafico` para so imprimir os numeros. O grafico traz `sqrt(h)` contra
`t` com a reta ajustada e, embaixo, `h(t)` contra a curva do modelo.

Se o programa avisar que o coeficiente angular saiu **positivo**, o `k` vem
negativo e nao tem sentido fisico: ou o CSV nao e o do esvaziamento, ou o
recorte pegou trecho de enchimento.

## Ensaio de degrau (Secao 2.3.3 do roteiro)

Primeiro, regule `PUMP2` por tentativa ate o nivel estabilizar em `h0` e anote
o comando (em %). Depois, com o mesmo comando como condicao inicial e um valor
cerca de 20 % acima como degrau:

```bash
python3 -W ignore 1_ensaio/registra_ensaio.py --degrau \
    --pump2-inicial 45 --pump2-final 65 --t-degrau 10 \
    -T 2 -d 180 -o degrau.csv
```

- `--t-degrau` garante o trecho de regime **antes** do degrau (as linhas de
  `t` negativo da Tab. 2.4). Atencao: o `t_s` do CSV conta do inicio da
  gravacao, entao para preencher a tabela subtraia `--t-degrau` (com
  `--t-degrau 10`, o `t = 10 s` do CSV e o `t = 0` da tabela);
- `-T 2` de novo pelo motivo do roteiro: em `h0 = 30 mm` a constante de tempo
  e da ordem de 20 s, e o acomodamento (~4 tau) leva perto de 80 s - por isso
  `-d 180`, que cobre a tabela inteira ate `t = +90 s`;
- `--valve` (padrao 100 %) e escrito **antes** de `PUMP2`, mantendo o
  [intertravamento de seguranca da Aula 1](../aula-01/README.md#seguranca-s-antes-de-pump2):
  a valvula `S` nunca fica fechada com a bomba acionada;
- `-d` encerra sozinho; sem ele, o ensaio vai ate o Ctrl+C.

## O CSV gerado

| coluna | unidade | o que e |
|---|---|---|
| `t_s` | s | tempo desde o inicio da gravacao |
| `lt_contas` | contas | leitura crua de `LT_ADC` |
| `h_mm` | mm | nivel, ja convertido pela calibracao |
| `ft2_contas` | contas | leitura crua de `FT2_ADC` |
| `qin_lpm` | L/min | vazao de entrada, ja convertida |
| `pump2_pct` | % | comando de `PUMP2` (vazio no ensaio de esvaziamento) |

Guardar as contas cruas ao lado dos valores convertidos permite recalcular todo
o ensaio se a calibracao de `LT` for corrigida depois.

## Testando longe do laboratorio

`--sim` substitui o CLP por um tanque de Torricelli simulado, com os mesmos
comandos e o mesmo CSV de saida:

```bash
python3 1_ensaio/registra_ensaio.py --sim -T 0.5 -d 30 -o teste.csv
python3 1_ensaio/registra_ensaio.py --sim --degrau \
    --pump2-inicial 41 --pump2-final 61 --t-degrau 5 -T 0.5 -d 40 -o teste.csv
```

Os parametros do simulador (area, `k`, vazao maxima) **nao sao medidos**, mas
estao calibrados para reproduzir os numeros do ensaio piloto do roteiro:
equilibrio em ~180 mm com `PUMP2` e `S` em 100 %, esvaziamento de 180 mm em
~50 s e `tau` ~ 20 s em `h0 = 30 mm` (que ali corresponde a `PUMP2` ~ 41 %).
Servem para exercitar o programa, nunca para substituir o ensaio.

## Sobre o periodo de amostragem

O laco desconta o tempo gasto na leitura antes de dormir, de modo que o periodo
efetivo seja `T` e nao `T + latencia`. Se alguma iteracao estourar `T`, o
programa avisa ao final quantas vezes isso aconteceu - sinal de que `T` esta
abaixo do piso imposto pela latencia de rede medida na Aula 1.
