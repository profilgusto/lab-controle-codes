"""Daemon que mantem a sessao EtherNet/IP aberta com o CLP e a expoe por TCP.

O MATLAB (ou qualquer cliente) conversa por JSON terminado em '\n':

    -> {"cmd": "ping"}
    <- {"ok": true, "result": "pong"}

    -> {"cmd": "info"}
    <- {"ok": true, "result": {"product_name": "1769-L24ER-QB1B/A ...", ...}}

    -> {"cmd": "tags"}                       (lista padrao da planta)
    <- {"ok": true, "result": ["Program:MainProgram.PUMP2_DAC", ...]}

    -> {"cmd": "read"}                       (sem "tags" = lista padrao)
    -> {"cmd": "read", "tags": ["Program:MainProgram.PT_ADC"]}
    <- {"ok": true, "result": [{"tag": "...", "value": 1234, "error": null}]}

    -> {"cmd": "write", "values": [["Program:MainProgram.VALVE_DAC", 12000]]}
    <- {"ok": true, "result": [{"tag": "...", "value": 12000, "error": null}]}

    -> {"cmd": "quit"}                       (encerra apenas esta conexao)

Uso:
    python3 -W ignore daemon_clp.py                    # 127.0.0.1:5020
    python3 -W ignore daemon_clp.py --port 5030 --plc-ip 200.200.200.25

A sessao CIP fica aberta entre requisicoes: o MATLAB paga so o round-trip da
leitura, e nao o handshake do EtherNet/IP a cada chamada. Varios clientes
podem se conectar ao mesmo tempo -- o acesso ao driver e serializado por um
lock, porque o LogixDriver nao e thread-safe.
"""

import argparse
import json
import logging
import socketserver
import threading

from pycomm3 import LogixDriver

PLC_IP_PADRAO = '200.200.200.25'
HOST_PADRAO = '127.0.0.1'
PORTA_PADRAO = 5020

TAGS_PADRAO = [
    'Program:MainProgram.PUMP2_DAC',
    'Program:MainProgram.VALVE_DAC',
    'Program:MainProgram.FT2_ADC',
    'Program:MainProgram.PT_ADC',
    'Program:MainProgram.TT5_ADC',
    'Program:MainProgram.LT_ADC',
]

INT_MIN = -32768
INT_MAX = 32767  # 2**15 - 1: o maior valor que cabe num INT do Logix

log = logging.getLogger('daemon_clp')


class ErroDeRequisicao(Exception):
    """Requisicao malformada -- devolve {"ok": false} sem derrubar a conexao."""


class SessaoCLP:
    """Mantem um LogixDriver aberto, reconectando sozinho quando cai."""

    def __init__(self, ip, checa_int=True):
        self.ip = ip
        self.checa_int = checa_int
        self._drv = None
        self._lock = threading.RLock()

    # -- ciclo de vida --------------------------------------------------
    def abre(self):
        with self._lock:
            if self._drv is None:
                log.info('conectando em %s ...', self.ip)
                drv = LogixDriver(self.ip)
                drv.open()
                self._drv = drv
                log.info('conectado: %s', drv.info.get('product_name'))
            return self._drv

    def fecha(self):
        with self._lock:
            if self._drv is not None:
                try:
                    self._drv.close()
                except Exception:
                    log.warning('falha ao fechar o driver', exc_info=True)
                finally:
                    self._drv = None

    def _executa(self, funcao):
        """Roda funcao(driver); se a sessao caiu, reconecta e tenta de novo."""
        with self._lock:
            try:
                return funcao(self.abre())
            except Exception as erro:
                log.warning('erro na sessao (%s) -- reconectando', erro)
                self.fecha()
                return funcao(self.abre())

    # -- operacoes ------------------------------------------------------
    def info(self):
        return self._executa(lambda drv: dict(drv.info))

    def le(self, tags):
        resultados = self._executa(lambda drv: drv.read(*tags))
        return [_tag_para_dict(r) for r in _lista(resultados)]

    def escreve(self, pares):
        if self.checa_int:
            for tag, valor in pares:
                _valida_int(tag, valor)
        tuplas = [(tag, valor) for tag, valor in pares]
        resultados = self._executa(lambda drv: drv.write(*tuplas))
        return [_tag_para_dict(r) for r in _lista(resultados)]


def _lista(resultados):
    """pycomm3 devolve um Tag para 1 tag e uma lista para varias."""
    return resultados if isinstance(resultados, list) else [resultados]


def _tag_para_dict(r):
    return {'tag': r.tag, 'value': r.value, 'type': r.type, 'error': r.error}


def _valida_int(tag, valor):
    """Pega o classico 32768 que estoura o INT e vira -32768 no CLP."""
    if isinstance(valor, bool) or not isinstance(valor, int):
        return
    if not INT_MIN <= valor <= INT_MAX:
        raise ErroDeRequisicao(
            f'{tag} = {valor} fora da faixa de um INT [{INT_MIN}, {INT_MAX}]. '
            f'Use --no-int-check se a tag nao for INT.')


# ----------------------------------------------------------------------
# Protocolo
# ----------------------------------------------------------------------
def trata_requisicao(sessao, req):
    if not isinstance(req, dict):
        raise ErroDeRequisicao('a requisicao deve ser um objeto JSON.')

    cmd = req.get('cmd')

    if cmd == 'ping':
        return 'pong'

    if cmd == 'info':
        return sessao.info()

    if cmd == 'tags':
        return list(TAGS_PADRAO)

    if cmd == 'read':
        tags = req.get('tags', TAGS_PADRAO)
        if isinstance(tags, str):
            tags = [tags]
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ErroDeRequisicao('"tags" deve ser uma lista de strings.')
        if not tags:
            raise ErroDeRequisicao('"tags" esta vazia.')
        return sessao.le(tags)

    if cmd == 'write':
        valores = req.get('values')
        if not isinstance(valores, list) or not valores:
            raise ErroDeRequisicao(
                '"values" deve ser uma lista nao vazia de pares [tag, valor].')
        pares = []
        for item in valores:
            if not isinstance(item, list) or len(item) != 2:
                raise ErroDeRequisicao(f'par invalido em "values": {item!r}')
            tag, valor = item
            if not isinstance(tag, str):
                raise ErroDeRequisicao(f'nome de tag invalido: {tag!r}')
            pares.append((tag, valor))
        return sessao.escreve(pares)

    raise ErroDeRequisicao(f'comando desconhecido: {cmd!r}')


class Manipulador(socketserver.StreamRequestHandler):

    def handle(self):
        log.info('cliente conectado: %s:%d', *self.client_address[:2])
        try:
            for linha in self.rfile:
                linha = linha.strip()
                if not linha:
                    continue

                try:
                    req = json.loads(linha)
                except json.JSONDecodeError as erro:
                    self._responde({'ok': False, 'error': f'JSON invalido: {erro}'})
                    continue

                if isinstance(req, dict) and req.get('cmd') == 'quit':
                    self._responde({'ok': True, 'result': 'bye'})
                    break

                try:
                    resultado = trata_requisicao(self.server.sessao, req)
                except ErroDeRequisicao as erro:
                    self._responde({'ok': False, 'error': str(erro)})
                except Exception as erro:
                    log.error('falha ao atender %r', req, exc_info=True)
                    self._responde({'ok': False,
                                    'error': f'{type(erro).__name__}: {erro}'})
                else:
                    self._responde({'ok': True, 'result': resultado})
        except (ConnectionResetError, BrokenPipeError):
            log.info('cliente desconectou abruptamente')
        finally:
            log.info('cliente encerrado: %s:%d', *self.client_address[:2])

    def _responde(self, obj):
        self.wfile.write(json.dumps(obj).encode('utf-8') + b'\n')
        self.wfile.flush()


class Servidor(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, endereco, sessao):
        super().__init__(endereco, Manipulador)
        self.sessao = sessao


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--plc-ip', default=PLC_IP_PADRAO,
                   help=f'IP do CLP (padrao: {PLC_IP_PADRAO})')
    p.add_argument('--host', default=HOST_PADRAO,
                   help=f'interface de escuta (padrao: {HOST_PADRAO}); '
                        f'use 0.0.0.0 para aceitar outra maquina')
    p.add_argument('--port', type=int, default=PORTA_PADRAO,
                   help=f'porta de escuta (padrao: {PORTA_PADRAO})')
    p.add_argument('--no-int-check', action='store_true',
                   help='nao valida a faixa de INT nas escritas')
    p.add_argument('--lazy', action='store_true',
                   help='so conecta no CLP na primeira requisicao')
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s  %(levelname)-7s %(message)s',
                        datefmt='%H:%M:%S')

    sessao = SessaoCLP(args.plc_ip, checa_int=not args.no_int_check)
    if not args.lazy:
        sessao.abre()

    with Servidor((args.host, args.port), sessao) as servidor:
        log.info('escutando em %s:%d  (Ctrl+C para encerrar)',
                 args.host, args.port)
        try:
            servidor.serve_forever()
        except KeyboardInterrupt:
            log.info('encerrando ...')
        finally:
            sessao.fecha()


if __name__ == '__main__':
    main()
