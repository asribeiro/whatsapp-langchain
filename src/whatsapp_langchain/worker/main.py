"""Entry point do Worker — loop de processamento de mensagens.

Inicia o Worker que consome mensagens da fila PostgreSQL em loop.
Cada mensagem é processada pelo agente configurado.

Uso:
    python -m whatsapp_langchain.worker.main
"""

import asyncio

import structlog

from whatsapp_langchain.shared.config import settings
from whatsapp_langchain.shared.db import (
    bootstrap_langgraph_schema,
    close_pool,
    get_pool,
    open_checkpointer,
    open_store,
    run_migrations,
)
from whatsapp_langchain.shared.observability import setup_logging
from whatsapp_langchain.worker.consumer import claim_next_message
from whatsapp_langchain.worker.processor import process_message
from whatsapp_langchain.worker.twilio_client import TwilioClient

logger = structlog.get_logger()


async def main() -> None:
    """Loop principal do Worker.

    1. Configura logging e banco de dados
    2. Aplica migrações pendentes
    3. Entra em loop infinito buscando mensagens na fila
    4. Processa cada mensagem com o agente apropriado
    """
    setup_logging(log_level=settings.log_level, json_output=settings.log_json)
    logger.info("worker_starting")

    pool = await get_pool()
    await run_migrations(pool)
    await bootstrap_langgraph_schema()

    checkpointer_stack, checkpointer = await open_checkpointer()

    store_stack, store = await open_store()

    outbound_mode = settings.resolved_twilio_outbound_mode
    if outbound_mode == "real":
        missing = []
        if not settings.twilio_account_sid:
            missing.append("TWILIO_ACCOUNT_SID")
        if not settings.twilio_api_key_sid:
            missing.append("TWILIO_API_KEY_SID")
        if not settings.twilio_api_key_secret:
            missing.append("TWILIO_API_KEY_SECRET")
        if not settings.twilio_from_number:
            missing.append("TWILIO_FROM_NUMBER")

        if missing:
            logger.error(
                "twilio_credentials_missing",
                missing=missing,
                outbound_mode=outbound_mode,
            )
            msg = (
                "Twilio outbound em modo real requer variáveis: "
                f"{', '.join(missing)}"
            )
            raise SystemExit(msg)

    twilio = TwilioClient(
        account_sid=settings.twilio_account_sid,
        api_key_sid=settings.twilio_api_key_sid,
        api_key_secret=settings.twilio_api_key_secret,
        from_number=settings.twilio_from_number,
        delivery_mode=outbound_mode,
    )
    logger.info(
        "twilio_client_ready",
        outbound_mode=outbound_mode,
        from_number=settings.twilio_from_number or None,
    )

    logger.info(
        "worker_ready",
        poll_interval=settings.poll_interval_seconds,
        memory_enabled=store is not None,
    )

    try:
        while True:
            message = await claim_next_message(pool, settings.lease_seconds)

            if message is None:
                await asyncio.sleep(settings.poll_interval_seconds)
                continue

            await process_message(
                message,
                pool,
                checkpointer=checkpointer,
                store=store,
                twilio=twilio,
            )

    except KeyboardInterrupt:
        logger.info("worker_interrupted")
    finally:
        if store_stack is not None:
            await store_stack.aclose()
        await checkpointer_stack.aclose()
        await close_pool()
        logger.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
