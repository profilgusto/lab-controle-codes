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
- **Leitura de todas as tags**: `PUMP2`, `VALVE`, `FT2`, `PT`, `TT5` e `LT`,
  em contas, volts e unidade de engenharia - `LT` ja em **mm**. O botao
  **"Ajustar calibracao de LT"** deste painel e o que define qual calibracao
  vale na sessao: ate ser usado, vale a da biblioteca
  (`LT_COEFS_H_DE_CONTAS`, de `comum/conversoes.py`, levantada numa bancada
  piloto); depois de colados os coeficientes da Aula 1, vale a da sua
  bancada. O rotulo abaixo do botao diz sempre qual das duas esta em vigor.
- **Grafico ao vivo**: `LT`, `FT2`, `PUMP2` e `VALVE`, todos convertidos para
  `%` do fundo de escala do instrumento (`conta_para_percentual`, de
  `comum/conversoes.py`) - por isso cabem no mesmo eixo, apesar de serem
  grandezas diferentes (nivel, vazao, dois comandos). Junto deles, a curva
  **`h` (nivel, mm)**, num eixo vertical proprio a direita: e a mesma leitura
  de `LT`, so que na grandeza fisica, e e ela que se le nos ensaios das Aulas
  2 e 3. A largura da janela de tempo se escolhe no seletor (30 s a 30 min).
- **Barra de status**: mostra a leitura corrente (com `LT` em mm) quando
  nenhum ensaio automatico esta em curso, ou o nome do ensaio que tomou o
  controle da planta.

### Uma unica conversao contas -> mm

Todo `h` que o hub mostra ou grava passa por `contas_para_altura_ativa`: a
tabela de leituras, a curva do grafico, o CSV/PDF da exportacao por recorte e
os CSV gravados pelas abas das Aulas 2 e 3. Trocar a calibracao no botao vale
imediatamente para todos eles (ensaios ja gravados nao sao reescritos - a
calibracao vale a partir dali). Por isso os ensaios das Aulas 2 e 3 pedem
confirmacao antes de comecar se nenhuma calibracao tiver sido colada na
sessao: gravar uma escada inteira com os coeficientes piloto produz um CSV
plausivel numa escala de nivel que nao e a desta bancada.

## Abas

| aba | o que tem |
|---|---|
| **Aula 1** | assistente de calibracao de `LT` (Secao 1.3.4) - adiciona pontos com a leitura atual de `LT` e o `h` medido na regua, salva CSV no formato de `ferramentas/calibracao-lt/calibra_lt.py` e ja mostra RMSE/erro maximo dos graus 1-3 e os coeficientes prontos para colar em "Ajustar calibracao de LT" (e em `LT_COEFS_H_DE_CONTAS`). A leitura das tags nao esta mais aqui: virou painel fixo, ao lado do grafico |
| **Aula 2** | ensaio de esvaziamento (Secao 2.3.2): grava um CSV a partir do instante em que a gravacao comeca, sem atuar em nada; ensaio de degrau (Secao 2.3.3): aplica a condicao inicial, toma o controle de `VALVE`/`PUMP2` (os sliders ficam bloqueados), aplica o degrau no instante programado e devolve o controle ao final. Os dois geram um CSV com as colunas `t_s, lt_contas, h_mm, ft2_contas, qin_lpm, pump2_pct`, com `h_mm` pela calibracao ativa |
| **Aula 3** | varredura estatica do atuador (Secao 3.3.1): percorre uma lista de comandos de `PUMP2` (crescente e depois decrescente) e grava, por patamar, a media de `FT2` e a de `h` (`u_pct, sentido, qin_lpm, h_mm`) - a coluna de `h` e o que mostra que `qin(u)` nao depende do nivel, ja que o tanque enche durante a varredura; escada de degraus (Secao 3.3.2): aplica uma sequencia configuravel de comandos, cada um mantido pela mesma duracao, gravando um CSV continuo (mesmas colunas da Aula 2) e, com o sufixo `_equilibrios`, um segundo CSV com uma linha por patamar (`patamar, u_pct, t_inicio_s, h_eq_mm, qin_eq_lpm`) - consumido por `ferramentas/identificacao-degrau/identifica_degrau.py` |
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
