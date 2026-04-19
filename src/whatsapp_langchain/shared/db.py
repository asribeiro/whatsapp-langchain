"""Pool de conexões PostgreSQL e utilitários de banco de dados.

Gerencia um pool singleton de conexões assíncronas usando psycopg.
O pool é criado no startup da aplicação (lifespan) e fechado no shutdown.

Uso:
    from whatsapp_langchain.shared.db import get_pool, close_pool, run_migrations

    # No lifespan da aplicação:
    pool = await get_pool()
    await run_migrations(pool)
    # ... app roda ...
    await close_pool()
"""

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path

import structlog
from langchain_openai import OpenAIEmbeddings
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from langgraph.store.postgres.base import PostgresIndexConfig
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from whatsapp_langchain.shared.config import settings

logger = structlog.get_logger()

# Singleton do pool de conexões
pool: AsyncConnectionPool | None = None
MIGRATIONS_LOCK_ID = 8_642_000
LANGGRAPH_BOOTSTRAP_LOCK_ID = 8_642_001


def _resolve_migrations_dir() -> Path:
    """Resolve o diretório de migrações para dev local e Docker.

    Em desenvolvimento com código-fonte, `__file__` aponta para:
    `.../src/whatsapp_langchain/shared/db.py` e o caminho relativo funciona.

    Em Docker com pacote instalado no site-packages, as migrações ficam em
    `/app/db/migrations`, então usamos cwd como fallback.
    """
    candidates = [
        # Dev local (código em src/)
        Path(__file__).resolve().parents[3] / "db" / "migrations",
        # Docker/execução a partir do WORKDIR do projeto
        Path.cwd() / "db" / "migrations",
        # Fallback explícito para imagem padrão deste projeto
        Path("/app/db/migrations"),
    ]

    for candidate in candidates:
        if candidate.is_dir():
            return candidate

    # Mantém comportamento previsível mesmo se diretório estiver ausente
    return candidates[0]


# Diretório de migrações
MIGRATIONS_DIR = _resolve_migrations_dir()


async def _acquire_advisory_lock(conn: AsyncConnection, lock_id: int) -> None:
    """Adquire advisory lock sem deixar transação ociosa bloqueando DDL concorrente."""
    while True:
        cursor = await conn.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
        locked = (await cursor.fetchone())[0]
        await conn.commit()
        if locked:
            return
        await asyncio.sleep(0.1)


async def _release_advisory_lock(conn: AsyncConnection, lock_id: int) -> None:
    """Libera advisory lock e encerra a transação do comando."""
    await conn.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
    await conn.commit()


async def get_pool() -> AsyncConnectionPool:
    """Retorna o pool de conexões, criando se necessário.

    O pool é singleton — chamadas subsequentes retornam a mesma instância.

    Returns:
        Pool de conexões assíncronas do psycopg.
    """
    global pool
    if pool is None:
        pool = AsyncConnectionPool(
            conninfo=settings.database_url,
            min_size=2,
            max_size=10,
            open=False,
        )
        await pool.open()
        db_host = settings.database_url.split("@")[-1]
        logger.info("db_pool_created", database_url=db_host)
    return pool


async def close_pool() -> None:
    """Fecha o pool de conexões.

    Chamado no shutdown da aplicação para liberar recursos.
    """
    global pool
    if pool is not None:
        await pool.close()
        pool = None
        logger.info("db_pool_closed")


async def run_migrations(db_pool: AsyncConnectionPool) -> None:
    """Aplica migrações SQL pendentes ao banco de dados.

    Lê arquivos de db/migrations/ e aplica os que ainda não foram
    registrados na tabela _migrations.

    Args:
        db_pool: Pool de conexões do psycopg.
    """
    async with db_pool.connection() as conn:
        conn: AsyncConnection
        await _acquire_advisory_lock(conn, MIGRATIONS_LOCK_ID)

        try:
            # Garante que a tabela de controle existe
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS _migrations (
                    id          SERIAL PRIMARY KEY,
                    name        TEXT NOT NULL UNIQUE,
                    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            await conn.commit()

            # Busca migrações já aplicadas
            cursor = await conn.execute("SELECT name FROM _migrations ORDER BY name")
            rows = await cursor.fetchall()
            applied = {row[0] for row in rows}

            # Lê e aplica migrações pendentes
            sql_files = sorted(MIGRATIONS_DIR.glob("*.sql"))

            for sql_file in sql_files:
                if sql_file.name in applied:
                    logger.debug("migration_already_applied", migration=sql_file.name)
                    continue

                logger.info("migration_applying", migration=sql_file.name)
                sql = sql_file.read_text(encoding="utf-8")
                await conn.execute(sql.encode())
                await conn.execute(
                    "INSERT INTO _migrations (name) VALUES (%s)",
                    (sql_file.name,),
                )
                await conn.commit()
                logger.info("migration_applied", migration=sql_file.name)
        finally:
            await _release_advisory_lock(conn, MIGRATIONS_LOCK_ID)


def resolve_store_index_config() -> PostgresIndexConfig:
    """Monta configuração de embeddings para o AsyncPostgresStore."""
    api_key = settings.openrouter_api_key
    secret_key = SecretStr(api_key.get_secret_value()) if api_key else None
    embeddings = OpenAIEmbeddings(
        model=settings.embedding_model,
        base_url=settings.openrouter_base_url,
        api_key=secret_key,
    )

    return {
        "embed": embeddings,
        "dims": settings.embedding_dims,
        "fields": ["$"],
    }


async def open_checkpointer() -> tuple[AsyncExitStack, AsyncPostgresSaver]:
    """Abre checkpointer PostgreSQL com ciclo de vida explícito."""
    stack = AsyncExitStack()
    checkpointer = await stack.enter_async_context(
        AsyncPostgresSaver.from_conn_string(settings.database_url)
    )
    return stack, checkpointer


async def open_store() -> tuple[AsyncExitStack, AsyncPostgresStore] | tuple[None, None]:
    """Abre store vetorial condicionalmente.

    O store só é criado quando MEMORY_ENABLED=true.
    """
    if not settings.memory_enabled:
        logger.info("store_skipped", reason="memory_disabled")
        return None, None

    stack = AsyncExitStack()
    store = await stack.enter_async_context(
        AsyncPostgresStore.from_conn_string(
            settings.database_url,
            index=resolve_store_index_config(),
        )
    )
    return stack, store


async def bootstrap_langgraph_schema() -> None:
    """Inicializa tabelas do checkpointer/store do LangGraph no startup.

    Isso evita criação lazy de schema durante o processamento da primeira
    mensagem e garante que o serviço só entra em estado ready após bootstrap.
    Um advisory lock serializa esse bootstrap entre API e Worker em banco limpo.
    """
    logger.info(
        "langgraph_schema_bootstrap_starting",
        memory_enabled=settings.memory_enabled,
    )
    db_pool = await get_pool()

    async with db_pool.connection() as conn:
        await _acquire_advisory_lock(conn, LANGGRAPH_BOOTSTRAP_LOCK_ID)

        try:
            checkpointer_stack, checkpointer = await open_checkpointer()
            store_stack, store = await open_store()

            try:
                await checkpointer.setup()
                if store is not None:
                    await store.setup()
            finally:
                if store_stack is not None:
                    await store_stack.aclose()
                await checkpointer_stack.aclose()
        finally:
            await _release_advisory_lock(conn, LANGGRAPH_BOOTSTRAP_LOCK_ID)

    logger.info("langgraph_schema_bootstrap_done")


async def check_db_health() -> bool:
    """Verifica se o banco de dados está acessível.

    Executa SELECT 1 para confirmar conectividade.

    Returns:
        True se o banco respondeu, False caso contrário.
    """
    try:
        db_pool = await get_pool()
        async with db_pool.connection() as conn:
            await conn.execute("SELECT 1")
        return True
    except Exception as e:
        logger.error("db_health_check_failed", error=str(e))
        return False
