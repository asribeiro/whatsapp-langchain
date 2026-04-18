# Arquitetura

Este projeto ensina agentes por uma perspectiva de **harness operacional**.
O agente é só uma parte da solução. O valor real está no harness completo:
entrada confiável, processamento assíncrono, persistência, recuperação de falhas e inspeção operacional.

## Estado Atual

Implementado:
- API FastAPI com webhook Twilio assíncrono (`POST /webhook/twilio`)
- validação criptográfica real de `X-Twilio-Signature` com SDK oficial do Twilio
- fila em PostgreSQL (`message_queue`) com debounce texto-only, flush antes de mídia e lease
- worker assíncrono consumindo fila com `FOR UPDATE SKIP LOCKED`
- cliente outbound Twilio com autenticação via API Key
- typing indicator best-effort antes da execução do agente
- envio da resposta para o WhatsApp via Twilio antes de `mark_done`
- execução de agentes via loader dinâmico
- checkpointer PostgreSQL (contexto por `thread_id`)
- store semântico PostgreSQL (memória por `user_id`)
- middleware de contexto (`trim`, `summarize`, `none`)
- memória semântica orientada a tools (`save_memory` e `read_memory`)
- processamento de mídia (imagem e áudio) via OpenRouter
- retry com backoff progressivo e status de falha
- APIs administrativas para inspeção
- frontend/admin panel em Next.js
- autenticação administrativa com Better Auth no schema `auth`
- proteção das rotas `/api/*` com `INTERNAL_SERVICE_TOKEN`
- deploy documentado em Railway
- stress testing documentado

Limitações conhecidas:
- `NumMedia > 1` no mesmo webhook continua fora do escopo
- o fechamento operacional completo ainda depende de número real Twilio + smoke final

## Visão do Harness

![Arquitetura](diagrams/harness_whatsapp.jpg)

```text
[Twilio/WhatsApp]
      |
      v
[Frontend Next.js]
  - Better Auth
  - server-side fetch para /api/*
      |
      v
[API FastAPI]
  - valida entrada
  - rate limit
  - enqueue/debounce
      |
      v
[PostgreSQL]
  - message_queue
  - conversations
  - checkpoints (langgraph)
  - store semântico (langgraph)
      |
      v
[Worker]
  - claim com lease
  - processa mídia
  - envia typing
  - invoca agente
  - envia resposta via Twilio
  - marca done/failed

[PostgreSQL auth]
  - schema auth
  - user/session/account
```

## Fronteiras e Contratos do Harness

### API (`src/whatsapp_langchain/server/`)

Responsabilidades:
- aceitar webhook Twilio
- validar assinatura quando habilitada
- responder rápido com TwiML vazio
- não executar agente inline
- enfileirar payload normalizado
- proteger `/api/*` via token interno compartilhado com o frontend

Contratos relevantes:
- `agent` via query string
- payload form-encoded Twilio (`From`, `To`, `Body`, `NumMedia`, etc)
- identidade inbound principal em `From`, com fallback para `WaId`
- `thread_id = "{phone}:{agent}"`

### Worker (`src/whatsapp_langchain/worker/`)

Responsabilidades:
- fazer polling da fila
- processar mídia se existir
- enviar typing antes do agente (best-effort)
- carregar agente com checkpointer/store compartilhados (abertos no boot)
- invocar grafo com `thread_id` e `user_id`
- enviar resposta ao usuário via Twilio
- persistir sucesso/falha, com `mark_done` só após envio confirmado

Contrato de execução do agente:
- `thread_id`: memória de conversa (checkpointer)
- `user_id`: memória cross-thread (store semântico), derivado do telefone do webhook Twilio

### Frontend (`frontend/`)

Responsabilidades:
- autenticar administradores com Better Auth
- consumir rotas administrativas apenas server-side
- usar `INTERNAL_API_URL` + `INTERNAL_SERVICE_TOKEN` para falar com a API
- manter tabelas de auth separadas no schema `auth`

### Shared (`src/whatsapp_langchain/shared/`)

Responsabilidades:
- configurações tipadas (`Settings`)
- pool/migrações
- operações de fila
- modelos Pydantic
- logging estruturado
- factory de LLM com rate limiter

## Modelo de Dados

### `message_queue`

Estado da mensagem e ciclo operacional.

Fluxo de status:

```text
queued -> processing -> done
                    -> queued (retry)
                    -> failed
```

Campos importantes:
- `process_after`: debounce e atraso de retry
- `lease_until`: lock temporal para worker
- `attempts` / `max_attempts`: governança de retry
- `response` / `error`: auditoria de resultado

### `conversations`

Tabela agregada para consultas administrativas.
- chave lógica: `(phone_number, agent_id)`
- atualizada por `upsert` a cada mensagem concluída

## Fluxo End-to-End

1. Usuário envia mensagem no WhatsApp.
2. Twilio faz `POST /webhook/twilio?agent=<agent_id>`.
3. API valida agente, assinatura (quando habilitada), aplica rate limit e chama `enqueue_or_buffer`.
4. Debounce concatena textos rápidos; mídia entra imediata e faz flush de texto pendente.
5. Worker faz `claim_next` com lease.
6. Worker pré-processa a entrada e monta `HumanMessage` (texto, imagem ou transcrição de áudio).
7. Worker tenta enviar typing via Twilio (best-effort).
8. Worker carrega agente com:
   - `AsyncPostgresSaver` (checkpointer) aberto no startup do worker
   - `AsyncPostgresStore` + embeddings (quando memória habilitada), também aberto no startup
9. Agente executa e retorna resposta.
10. Worker envia a resposta outbound via Twilio.
11. Só depois o worker persiste resultado (`mark_done`) e atualiza `conversations`.
12. Em erro de processamento ou envio, `mark_failed` decide retry com backoff ou falha final.

## Contexto e Memória

### Contexto por thread (checkpointer)

Persistência de mensagens de uma conversa específica (`thread_id`).

### Memória semântica por usuário (store)

- namespace: `(user_id, "memories")`
- `save_memory` grava fatos relevantes
- `read_memory` recupera memórias por similaridade quando o agente precisar
- `user_id` no runtime vem de `phone_number` (payload Twilio)
- não usamos escopo `tenant_user`/`tenant_shared` neste projeto

Isso separa duas necessidades diferentes:
- continuidade da conversa atual
- conhecimento durável sobre o usuário

## Controles do Harness

### Debounce

Agrupa mensagens de texto enviadas em sequência curta (`MESSAGE_BUFFER_SECONDS`) para reduzir custo e ruído.

No estado atual do projeto:
- texto faz debounce
- mídia não faz debounce
- antes de inserir mídia, textos pendentes do mesmo `phone+agent` são flushed
- concorrência do mesmo remetente/agente é serializada com advisory lock

### Retry com backoff

`mark_failed` aplica `backoff_seconds = attempts * 5` enquanto houver tentativas.

### Rate limits

- API: limite por telefone/hora (in-memory)
- LLM: token bucket por processo (`InMemoryRateLimiter`)

### Observabilidade

Logs estruturados com `structlog` em todos os componentes.

## Endpoints Disponíveis

- `GET /health`
- `POST /webhook/twilio?agent=<id>`
- `POST /webhook/sync?agent=<id>` (educacional)
- `GET /api/agents`
- `GET /api/chats`
- `GET /api/chats/{phone_number}`
- `GET /api/metrics`

## Decisões do Harness (didáticas)

- PostgreSQL como fila: reduz moving parts no início.
- API e Worker separados: isola latência da IA da borda HTTP.
- Loader dinâmico de agentes: facilita catálogo e extensibilidade.
- Config centralizada: evita divergência de comportamento por módulo.
- Middleware explícito: torna política de contexto auditável.
- Memória por tools explícitas: separa contexto transiente (middleware) de memória durável (store).

## Próximos passos do Harness

- fechar o teste e2e real com número Twilio final
- endurecer operação multi-instância (rate limit distribuído)
- revisar proteção de admin/CORS para produção
- suporte explícito a cenários fora do escopo atual, como `NumMedia > 1`
