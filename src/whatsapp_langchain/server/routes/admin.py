"""Rotas administrativas para o painel de controle.

Endpoints para visualizar conversas, métricas e agentes disponíveis.
Usado pelo frontend (Next.js Admin Panel).

Uso:
    curl http://localhost:8000/api/agents
    curl http://localhost:8000/api/chats?limit=20
    curl http://localhost:8000/api/chats/+5511999999999
    curl http://localhost:8000/api/metrics
"""

import structlog
from fastapi import APIRouter, Depends, Query

from whatsapp_langchain.agents.loader import list_agents
from whatsapp_langchain.server.dependencies import verify_service_token
from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.opt_out import list_whatsapp_opt_outs

logger = structlog.get_logger()

router = APIRouter(
    prefix="/api",
    tags=["admin"],
    dependencies=[Depends(verify_service_token)],
)


@router.get("/agents")
async def get_agents() -> dict[str, list[str]]:
    """Lista agentes disponíveis no catálogo.

    Returns:
        Lista de agent_ids registrados.
    """
    return {"agents": list_agents()}


@router.get("/whatsapp/suppressions")
async def get_whatsapp_suppressions() -> dict:
    """Expõe um snapshot protegido dos opt-outs para o pipeline de audiência."""
    pool = await get_pool()
    return await list_whatsapp_opt_outs(pool)


@router.get("/chats")
async def get_chats(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Lista conversas ativas, ordenadas por última mensagem.

    Args:
        limit: Máximo de resultados (1-100). Default: 20.
        offset: Offset para paginação. Default: 0.

    Returns:
        Lista de conversas com paginação.
    """
    pool = await get_pool()

    async with pool.connection() as conn:
        cursor = await conn.execute(
            """
            SELECT phone_number, agent_id, thread_id, last_message,
                   last_message_at, message_count, created_at
            FROM conversations
            ORDER BY last_message_at DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )
        rows = await cursor.fetchall()

        # Total para paginação
        count_cursor = await conn.execute("SELECT COUNT(*) FROM conversations")
        count_row = await count_cursor.fetchone()
        total = count_row[0] if count_row else 0

    chats = [
        {
            "phone_number": row[0],
            "agent_id": row[1],
            "thread_id": row[2],
            "last_message": row[3],
            "last_message_at": row[4].isoformat() if row[4] else None,
            "message_count": row[5],
            "created_at": row[6].isoformat() if row[6] else None,
        }
        for row in rows
    ]

    return {"chats": chats, "total": total, "limit": limit, "offset": offset}


@router.get("/chats/{phone_number:path}")
async def get_chat_messages(
    phone_number: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Lista mensagens de uma conversa específica.

    Args:
        phone_number: Número de telefone do remetente.
        limit: Máximo de resultados (1-200). Default: 50.
        offset: Offset para paginação. Default: 0.

    Returns:
        Lista de mensagens da conversa.
    """
    pool = await get_pool()

    async with pool.connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, agent_id, incoming_message, media_type,
                   normalized_input, media_processing_status,
                   response, status, created_at, processed_at,
                   media_processing_error, error
            FROM message_queue
            WHERE phone_number = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
            """,
            (phone_number, limit, offset),
        )
        rows = await cursor.fetchall()

    messages = [
        {
            "id": row[0],
            "agent_id": row[1],
            "incoming_message": row[2],
            "media_type": row[3],
            "normalized_input": row[4],
            "media_processing_status": row[5],
            "response": row[6],
            "status": row[7],
            "created_at": row[8].isoformat() if row[8] else None,
            "processed_at": row[9].isoformat() if row[9] else None,
            "media_processing_error": row[10],
            "error": row[11],
        }
        for row in rows
    ]

    return {"phone_number": phone_number, "messages": messages}


@router.get("/metrics")
async def get_metrics() -> dict:
    """Métricas operacionais da fila de mensagens.

    Returns:
        Métricas: total hoje, falhas, tempo médio de processamento, fila atual.
    """
    pool = await get_pool()

    async with pool.connection() as conn:
        # Total de mensagens hoje
        cursor = await conn.execute(
            """
            SELECT COUNT(*) FROM message_queue
            WHERE created_at >= CURRENT_DATE
            """
        )
        row = await cursor.fetchone()
        total_today = row[0] if row else 0

        # Falhas hoje
        cursor = await conn.execute(
            """
            SELECT COUNT(*) FROM message_queue
            WHERE status = 'failed' AND created_at >= CURRENT_DATE
            """
        )
        row = await cursor.fetchone()
        failures_today = row[0] if row else 0

        # Tempo médio de processamento (em segundos)
        cursor = await conn.execute(
            """
            SELECT AVG(EXTRACT(EPOCH FROM (processed_at - created_at)))
            FROM message_queue
            WHERE status = 'done' AND processed_at IS NOT NULL
              AND created_at >= CURRENT_DATE
            """
        )
        row = await cursor.fetchone()
        avg_processing_time = (
            float(round(row[0], 2)) if row and row[0] is not None else None
        )

        # Mensagens na fila agora
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM message_queue WHERE status = 'queued'"
        )
        row = await cursor.fetchone()
        queue_size = row[0] if row else 0

    return {
        "total_today": total_today,
        "failures_today": failures_today,
        "avg_processing_time_seconds": avg_processing_time,
        "queue_size": queue_size,
    }


@router.get("/queue")
async def get_queue() -> dict:
    """Visão geral da fila de mensagens: contadores por status e mensagens recentes.

    Usado pelo painel admin para monitorar o estado da fila em tempo real.
    Retorna contadores do dia atual agrupados por status e as últimas 50 mensagens.

    Returns:
        Contadores por status (queued, processing, done, failed) e lista
        das 50 mensagens mais recentes com dados resumidos.
    """
    pool = await get_pool()

    async with pool.connection() as conn:
        # Contadores por status (apenas do dia atual)
        cursor = await conn.execute(
            """
            SELECT status, COUNT(*) as count
            FROM message_queue
            WHERE created_at >= CURRENT_DATE
            GROUP BY status
            """
        )
        status_rows = await cursor.fetchall()

        # Inicializa todos os status com zero para garantir presença no response
        counters = {"queued": 0, "processing": 0, "done": 0, "failed": 0}
        for row in status_rows:
            counters[row[0]] = row[1]

        # Mensagens recentes (últimas 50, qualquer data)
        cursor = await conn.execute(
            """
            SELECT id, phone_number, agent_id,
                   LEFT(incoming_message, 100) as incoming_message,
                   status, created_at, attempts, error
            FROM message_queue
            ORDER BY created_at DESC
            LIMIT 50
            """
        )
        message_rows = await cursor.fetchall()

    messages = [
        {
            "id": row[0],
            "phone_number": row[1],
            "agent_id": row[2],
            "incoming_message": row[3],
            "status": row[4],
            "created_at": row[5].isoformat() if row[5] else None,
            "attempts": row[6],
            "error": row[7],
        }
        for row in message_rows
    ]

    logger.debug(
        "queue_status_fetched", counters=counters, messages_count=len(messages)
    )

    return {"counters": counters, "messages": messages}
