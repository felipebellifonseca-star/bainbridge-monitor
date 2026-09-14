# Monitor gratuito - Bainbridge The Grand

Monitora as plantas **B1 e B2** em:

- https://bainbridgegrand.com/floorplans/b1/
- https://bainbridgegrand.com/floorplans/b2/

Filtro atual: **aluguel inicial <= US$ 2.700/mês**.

O monitor verifica automaticamente aproximadamente a cada 5 minutos e envia Telegram quando:

- aparece uma B1 ou B2 dentro do limite;
- uma unidade some;
- o preço muda;
- uma unidade acima de US$ 2.700 cai para dentro do limite;
- uma unidade passa de US$ 2.700;
- a data/status de disponibilidade muda;
- andar ou metragem publicados mudam.

No primeiro teste, o bot envia a lista completa atual para confirmar que tudo está funcionando.

## Por que o repositório deve ser público?

GitHub Actions em runner padrão é gratuito e ilimitado para repositórios públicos.
Não coloque o token do Telegram em nenhum arquivo: ele deve ficar em **GitHub Secrets**.

## Configuração

### 1. Crie o bot no Telegram

1. Abra o Telegram.
2. Procure `@BotFather`.
3. Envie `/newbot`.
4. Escolha um nome.
5. Escolha um username terminado em `bot`.
6. Copie o token fornecido.
7. Abra o bot que você acabou de criar.
8. Toque em **Start** e envie `/start`.

### 2. Crie um repositório público no GitHub

Sugestão de nome:

`bainbridge-monitor`

Defina como **Public**.

### 3. Envie estes arquivos para o repositório

Envie o CONTEÚDO desta pasta, mantendo:

- `monitor.py`
- `requirements.txt`
- `state.json`
- `.github/workflows/monitor.yml`

### 4. Salve o token como Secret

No repositório:

**Settings > Secrets and variables > Actions > New repository secret**

Nome:

`TELEGRAM_BOT_TOKEN`

Valor:

o token recebido do BotFather.

Não precisa configurar `TELEGRAM_CHAT_ID` no começo. O script tenta encontrar automaticamente
o chat privado mais recente que enviou mensagem ao bot.

Se quiser travar o bot explicitamente em um chat depois, crie também o secret:

`TELEGRAM_CHAT_ID`

### 5. Teste agora, sem esperar 15 minutos

Vá em:

**Actions > Monitor Bainbridge > Run workflow**

Na primeira execução você deverá receber no Telegram:

`✅ MONITOR BAINBRIDGE ATIVADO`

junto com todas as B1 e B2 que naquele momento custarem até US$ 2.700,
incluindo andar, preço e quando estarão disponíveis.

Depois disso, se nada mudar, o bot fica em silêncio.

## Intervalo

O cron está configurado em:

`*/5 * * * *`

Isto significa aproximadamente:

- XX:07
- XX:22
- XX:37
- XX:52

O GitHub pode atrasar alguns minutos uma execução agendada; não é um relógio de precisão.

## Alterar o preço máximo

Em `.github/workflows/monitor.yml`, altere:

`MAX_RENT: "2700"`

Exemplo para US$ 2.800:

`MAX_RENT: "2800"`

## Segurança

O repositório pode ser público porque não contém credenciais.
O `TELEGRAM_BOT_TOKEN` fica guardado em GitHub Secrets e não deve ser colocado no código,
README, state.json ou commits.

## Keepalive

O script não cria commit a cada 15 minutos. Ele só atualiza `state.json` quando os dados
do site mudam. Se nada mudar por 30 dias, faz um pequeno keepalive para manter o
repositório ativo e evitar que o agendamento público fique inativo por falta de atividade.
