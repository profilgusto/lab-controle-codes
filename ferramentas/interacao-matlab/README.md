# interacao-matlab

Ponte entre o MATLAB e o CLP Allen-Bradley (TQ CE117, `200.200.200.25`) sem
reimplementar EtherNet/IP: um daemon Python segura a sessao CIP aberta com o
`pycomm3` e a expoe numa porta TCP local; o MATLAB conversa por JSON usando
`tcpclient`, que e do MATLAB base -- nenhuma toolbox necessaria.

```
MATLAB  --(TCP 5020, JSON/LF)-->  daemon_clp.py  --(EtherNet/IP)-->  CLP
```

## Arquivos

| arquivo | o que faz |
|---|---|
| `daemon_clp.py` | servidor TCP que mantem o `LogixDriver` aberto, serializa o acesso e reconecta sozinho se a sessao cair |
| `exemplo_clp.m` | script MATLAB: le as tags, escreve nos DACs, le de volta e faz uma aquisicao periodica com grafico |

## Como usar

1. **Terminal** (no ambiente que ja tem o `pycomm3` instalado):

   ```bash
   python3 -W ignore daemon_clp.py
   ```

   Opcoes: `--plc-ip`, `--host`, `--port`, `--lazy` (so conecta na primeira
   requisicao) e `--no-int-check` (desliga a validacao da faixa de INT).
   Para acessar de outra maquina, suba com `--host 0.0.0.0`.

2. **MATLAB**:

   ```matlab
   >> cd interacao-matlab
   >> exemplo_clp
   ```

## Protocolo

Uma linha de JSON por requisicao, terminada em `\n`; a resposta vem no mesmo
formato, sempre com `ok` e depois `result` ou `error`.

| requisicao | resposta |
|---|---|
| `{"cmd":"ping"}` | `{"ok":true,"result":"pong"}` |
| `{"cmd":"info"}` | dados do controlador (`product_name`, `revision`, ...) |
| `{"cmd":"tags"}` | lista padrao de tags da planta |
| `{"cmd":"read"}` | le a lista padrao |
| `{"cmd":"read","tags":["Program:MainProgram.PT_ADC"]}` | `[{"tag":...,"value":...,"type":"INT","error":null}]` |
| `{"cmd":"write","values":[["Program:MainProgram.VALVE_DAC",12000]]}` | mesmo formato da leitura |
| `{"cmd":"quit"}` | encerra apenas aquela conexao |

Erros de tag individuais vem no campo `error` de cada item (a resposta ainda e
`ok: true`); erros de requisicao ou de comunicacao vem como `ok: false`.

## Notas

- **Faixa de INT**: escritas com inteiro fora de `[-32768, 32767]` sao
  recusadas pelo daemon, para pegar o classico `32768` que viraria `-32768`
  no CLP. Se for escrever num DINT ou REAL, suba com `--no-int-check`.
- **Concorrencia**: varios clientes podem se conectar ao mesmo tempo; o acesso
  ao driver e serializado por um lock, porque o `LogixDriver` nao e
  thread-safe.
- **Desempenho**: como a sessao CIP fica aberta, cada leitura custa so o
  round-trip do CIP (5 a 20 ms nesta planta) -- da para fechar malha no MATLAB
  com periodo de 0,1 a 1 s. Nao e tempo real deterministico.
- **Seguranca**: por padrao escuta so em `127.0.0.1`. O protocolo nao tem
  autenticacao -- ao expor com `--host 0.0.0.0`, restrinja por firewall.
