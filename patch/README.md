# Patch de Compatibilidade por Fase

Esta pasta guarda arquivos de substituição para as fases após a `Fase_1`.

Cada subpasta contém os arquivos que devem ser copiados sobre a tag
correspondente antes de rodar o ambiente local:

- `pyproject.toml`
- `uv.lock`
- `Dockerfile.api`
- `Dockerfile.worker`
- `src/whatsapp_langchain/agents/catalog/rhawk_assistant/graph.py`

Fluxo sugerido:

```bash
git checkout Fase_2
cp patch/Fase_2/pyproject.toml .
cp patch/Fase_2/uv.lock .
cp patch/Fase_2/Dockerfile.api .
cp patch/Fase_2/Dockerfile.worker .
cp patch/Fase_2/src/whatsapp_langchain/agents/catalog/rhawk_assistant/graph.py \
  src/whatsapp_langchain/agents/catalog/rhawk_assistant/graph.py
make setup
docker compose build --no-cache api worker
make dev
```

O patch de Docker é importante porque `make up`/`docker compose up` também precisa
respeitar o `uv.lock`. Sem isso, um clone limpo pode resolver transitivos mais
novos e incompatíveis, mesmo com `pyproject.toml` e `uv.lock` copiados.

Repita o mesmo padrão para `Fase_3` e `Fase_4`.
