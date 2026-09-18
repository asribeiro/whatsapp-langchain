"""Política e persistência de opt-out do WhatsApp."""

import re
import unicodedata
from datetime import UTC, datetime

from psycopg_pool import AsyncConnectionPool

OPT_OUT_COMMANDS = {
    "CANCEL",
    "CANCELAR",
    "PARAR",
    "REMOVER",
    "SAIR",
    "STOP",
    "UNSUBSCRIBE",
}


def normalize_opt_out_command(body: str) -> str:
    """Normaliza uma resposta curta sem transformar frases em comandos."""
    normalized = unicodedata.normalize("NFKD", body or "")
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^A-Z]", "", ascii_text.upper().strip())


def is_opt_out_command(body: str) -> bool:
    """Retorna True somente para comandos explícitos de descadastro."""
    return normalize_opt_out_command(body) in OPT_OUT_COMMANDS


async def record_whatsapp_opt_out(
    pool: AsyncConnectionPool,
    phone_number: str,
    message_id: str | None,
) -> None:
    """Persiste a supressão de forma idempotente."""
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO whatsapp_suppressions
                (phone_number, reason, source, message_id)
            VALUES (%s, 'user_opt_out', 'inbound_keyword', %s)
            ON CONFLICT (phone_number) DO UPDATE
            SET reason = EXCLUDED.reason,
                source = EXCLUDED.source,
                message_id = COALESCE(
                    EXCLUDED.message_id,
                    whatsapp_suppressions.message_id
                ),
                updated_at = NOW()
            """,
            (phone_number, message_id or None),
        )
        await conn.commit()


async def list_whatsapp_opt_outs(pool: AsyncConnectionPool) -> dict:
    """Retorna snapshot ordenado para materialização da audiência."""
    async with pool.connection() as conn:
        cursor = await conn.execute(
            """
            SELECT phone_number
            FROM whatsapp_suppressions
            ORDER BY phone_number
            """
        )
        rows = await cursor.fetchall()

    return {
        "observedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "phones": [row[0] for row in rows],
    }
