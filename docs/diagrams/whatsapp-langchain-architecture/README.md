# Diagramas da Arquitetura

Esta pasta reúne os diagramas de arquitetura do `whatsapp-langchain` em formato de imagem, prontos para consulta no repositório.

Os arquivos `PNG` abaixo são os artefatos principais para consulta:

1. [01-fluxo-de-dados.png](/Users/ronnald/Documents/code/pro/whatsapp-langchain/docs/diagrams/whatsapp-langchain-architecture/01-fluxo-de-dados.png)
Mostra o caminho completo da mensagem: entrada pelo webhook, enfileiramento, processamento pelo worker, execução do agente e envio da resposta.
2. [02-maquina-de-estados.png](/Users/ronnald/Documents/code/pro/whatsapp-langchain/docs/diagrams/whatsapp-langchain-architecture/02-maquina-de-estados.png)
Mostra os estados da mensagem na fila e as transições entre `queued`, `processing`, `done` e `failed`.
3. [03-concorrencia-e-locks.png](/Users/ronnald/Documents/code/pro/whatsapp-langchain/docs/diagrams/whatsapp-langchain-architecture/03-concorrencia-e-locks.png)
Mostra como o sistema controla concorrência no enqueue e no dequeue usando advisory locks e `SKIP LOCKED`.
4. [04-falha-e-recuperacao.png](/Users/ronnald/Documents/code/pro/whatsapp-langchain/docs/diagrams/whatsapp-langchain-architecture/04-falha-e-recuperacao.png)
Mostra os principais cenários de falha e a estratégia adotada em cada um: retry, degradação graciosa ou rejeição.
5. [05-modelos-de-dados.png](/Users/ronnald/Documents/code/pro/whatsapp-langchain/docs/diagrams/whatsapp-langchain-architecture/05-modelos-de-dados.png)
Mostra os papéis do PostgreSQL no projeto como banco relacional, document store e vector store.
6. [06-pipeline-do-agente.png](/Users/ronnald/Documents/code/pro/whatsapp-langchain/docs/diagrams/whatsapp-langchain-architecture/06-pipeline-do-agente.png)
Mostra o fluxo interno do agente: restauração de contexto, estratégia de histórico, chamada ao LLM, memória e persistência do estado.

Se você quiser uma leitura progressiva da arquitetura, a melhor ordem é `01 -> 02 -> 03 -> 04 -> 05 -> 06`.
