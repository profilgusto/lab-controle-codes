%% exemplo_clp.m -- le e escreve tags do CLP a partir do MATLAB
%
% Fala com o daemon_clp.py por TCP (JSON terminado em LF). Nao precisa de
% toolbox nenhuma: tcpclient, jsonencode e jsondecode sao do MATLAB base
% (tcpclient com writeline/readline exige R2020b ou mais novo).
%
% Antes de rodar, suba o daemon no terminal:
%
%     python3 -W ignore daemon_clp.py
%
% e entao, no MATLAB:
%
%     >> exemplo_clp
%
% O daemon segura a sessao EtherNet/IP aberta, entao cada leitura aqui custa
% so o round-trip do CIP -- tipicamente 5 a 20 ms nesta planta.

clear; clc;

HOST = "127.0.0.1";
PORTA = 5020;

TAG_VALVE = 'Program:MainProgram.VALVE_DAC';
TAG_PUMP2 = 'Program:MainProgram.PUMP2_DAC';

VALVE_ALVO = 12000;   % contas do DAC (0..32767)
PUMP2_ALVO =  8000;
ESPERA_S   = 2.0;

%% 1. conecta no daemon -------------------------------------------------
c = tcpclient(HOST, PORTA, "Timeout", 10);
configureTerminator(c, "LF");
% Para encerrar a conexao depois, basta:  clear c

fprintf('Daemon respondeu: %s\n', clp_cmd(c, struct("cmd", "ping")));

info = clp_cmd(c, struct("cmd", "info"));
if isfield(info, 'revision')
    fprintf('CLP: %s  (revisao %d.%d)\n\n', string(info.product_name), ...
            info.revision.major, info.revision.minor);
else
    fprintf('CLP: %s\n\n', string(info.product_name));
end

%% 2. leitura da lista padrao de tags -----------------------------------
disp('--- leitura inicial ---');
mostra(clp_read(c));

%% 3. escrita nos DACs ---------------------------------------------------
fprintf('\nEscrevendo VALVE_DAC = %d  |  PUMP2_DAC = %d\n', ...
        VALVE_ALVO, PUMP2_ALVO);
clp_write(c, {TAG_VALVE, VALVE_ALVO}, {TAG_PUMP2, PUMP2_ALVO});
fprintf('Escrita confirmada pelo CLP. Aguardando %.0f s...\n\n', ESPERA_S);
pause(ESPERA_S);

%% 4. leitura de volta ---------------------------------------------------
disp('--- leitura apos a atuacao ---');
mostra(clp_read(c));

%% 5. aquisicao periodica (exemplo de malha) -----------------------------
% Le PT e LT a cada 0,5 s por 10 amostras e plota. E o esqueleto de uma
% malha fechada: basta calcular a acao de controle e chamar clp_write aqui.
N  = 10;
TS = 0.5;
tags = {'Program:MainProgram.PT_ADC', 'Program:MainProgram.LT_ADC'};

t = zeros(N, 1);
y = zeros(N, numel(tags));

fprintf('\nAdquirindo %d amostras a cada %.1f s...\n', N, TS);
t0 = tic;
for k = 1:N
    r = clp_read(c, tags);
    t(k)    = toc(t0);
    y(k, :) = conta_para_volts([r.value]);
    fprintf('  t = %5.2f s   PT = %+6.3f V   LT = %+6.3f V\n', ...
            t(k), y(k, 1), y(k, 2));
    pause(max(0, TS - (toc(t0) - t(k))));
end

figure('Name', 'CLP TQ CE117');
plot(t, y, '-o'); grid on;
xlabel('tempo (s)'); ylabel('tensao (V)');
legend('PT\_ADC', 'LT\_ADC', 'Location', 'best');
title('Aquisicao via daemon EtherNet/IP');

%% ----------------------------------------------------------------------
%  Funcoes auxiliares
%% ----------------------------------------------------------------------
function resultado = clp_cmd(c, requisicao)
%CLP_CMD Envia uma requisicao JSON ao daemon e devolve o campo "result".
    writeline(c, jsonencode(requisicao));
    resposta = jsondecode(readline(c));
    if ~resposta.ok
        error('clp:daemon', 'Daemon recusou a requisicao: %s', resposta.error);
    end
    resultado = resposta.result;
end

function r = clp_read(c, tags)
%CLP_READ Le tags do CLP. Sem argumento, le a lista padrao da planta.
%   r e um struct array com os campos tag, value, type e error.
    if nargin < 2
        requisicao = struct("cmd", "read");
    else
        requisicao = struct("cmd", "read", "tags", {cellstr(tags)});
    end
    r = normaliza(clp_cmd(c, requisicao));
    checa_erros(r);
end

function r = clp_write(c, varargin)
%CLP_WRITE Escreve pares {tag, valor}.
%   clp_write(c, {'Program:MainProgram.VALVE_DAC', 12000})
    pares = cell(1, numel(varargin));
    for i = 1:numel(varargin)
        par = varargin{i};
        pares{i} = {char(par{1}), round(double(par{2}))};
    end
    r = normaliza(clp_cmd(c, struct("cmd", "write", "values", {pares})));
    checa_erros(r);
end

function r = normaliza(resultado)
%NORMALIZA jsondecode devolve struct array ou cell de structs; padroniza.
    if iscell(resultado)
        r = [resultado{:}];
    else
        r = resultado;
    end
end

function checa_erros(r)
%CHECA_ERROS O daemon devolve ok=true mesmo quando uma tag falha; o erro
%   vem por tag, no campo "error" (vazio quando deu certo).
    for i = 1:numel(r)
        if ~isempty(r(i).error)
            error('clp:tag', 'Falha na tag %s: %s', ...
                  string(r(i).tag), string(r(i).error));
        end
    end
end

function mostra(r)
%MOSTRA Imprime tag, contas e a tensao equivalente.
    fprintf('%-38s %12s %10s\n', 'TAG', 'CONTAS', 'VOLTS');
    fprintf('%s\n', repmat('-', 1, 62));
    for i = 1:numel(r)
        fprintf('%-38s %12d %+10.3f\n', string(r(i).tag), ...
                r(i).value, conta_para_volts(r(i).value));
    end
end

function v = conta_para_volts(contas)
%CONTA_PARA_VOLTS Cartao AD/DA: -32768..32767 contas <-> -10,5..+10,5 V.
    v = double(contas) * 10.5 / 32768;
end

function contas = volts_para_conta(volts) %#ok<DEFNU>
%VOLTS_PARA_CONTA Inverso de conta_para_volts, saturado na faixa do INT.
    contas = round(double(volts) * 32768 / 10.5);
    contas = min(max(contas, -32768), 32767);
end
