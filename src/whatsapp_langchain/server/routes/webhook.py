"""Webhook do Twilio — processamento assíncrono via fila.

Recebe mensagens do Twilio, valida, aplica rate limit, e coloca na fila
para processamento pelo Worker. Retorna 200 imediatamente (TwiML vazio).

Fluxo: Twilio -> POST /webhook/twilio -> Fila (PostgreSQL) -> Worker

Uso:
    curl -X POST ".../webhook/twilio?agent=rhawk_assistant" \
         -d "MessageSid=SM123&From=whatsapp:+5511..."
"""

from typing import Literal

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Response

from whatsapp_langchain.agents.loader import AgentNotFoundError, list_agents
from whatsapp_langchain.server.dependencies import (
    check_rate_limit,
    validate_twilio_signature,
)
from whatsapp_langchain.shared.config import settings
from whatsapp_langchain.shared.db import get_pool
from whatsapp_langchain.shared.opt_out import (
    is_opt_out_command,
    record_whatsapp_opt_out,
)
from whatsapp_langchain.shared.queue import enqueue_or_buffer

logger = structlog.get_logger()

router = APIRouter(tags=["webhook"])

# TwiML vazio — indica ao Twilio que recebemos a mensagem
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
OPT_OUT_TWIML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<Response><Message>Seu contato foi removido das nossas comunicações. "
    "Você não receberá novas mensagens promocionais.</Message></Response>"
)


@router.post("/webhook/twilio")
async def webhook_twilio(
    agent: str = Query(
        description="ID do agente para processar a mensagem",
    ),
    mode: Literal["agent", "campaign"] = Query(
        default="agent",
        description=(
            "Modo de entrada: agent encaminha ao agente; campaign processa "
            "apenas opt-out e ignora silenciosamente as demais respostas."
        ),
    ),
    message_sid: str = Form(
        default="",
        alias="MessageSid",
        description="ID da mensagem no Twilio (MessageSid).",
    ),
    from_number: str = Form(
        default="",
        alias="From",
        description="Número remetente no formato whatsapp:+55...",
    ),
    to_number_form: str = Form(
        default="",
        alias="To",
        description="Número de destino no formato whatsapp:+...",
    ),
    body: str = Form(
        default="",
        alias="Body",
        description="Texto da mensagem (pode ser vazio em mensagens de mídia).",
    ),
    num_media_raw: str = Form(
        default="0",
        alias="NumMedia",
        description="Quantidade de mídias anexadas.",
    ),
    media_url_form: str | None = Form(
        default=None,
        alias="MediaUrl0",
        description="URL da primeira mídia (quando NumMedia > 0).",
    ),
    media_type_form: str | None = Form(
        default=None,
        alias="MediaContentType0",
        description="MIME type da primeira mídia.",
    ),
    wa_id: str = Form(
        default="",
        alias="WaId",
        description="WhatsApp ID do remetente (fallback de From).",
    ),
    _signature: None = Depends(validate_twilio_signature),
) -> Response:
    """Recebe webhook do Twilio e enfileira para processamento.

    O Worker consome a mensagem da fila, executa o agente, e envia
    a resposta via Twilio. Assinatura validada via HMAC-SHA1 (SDK oficial)
    quando VALIDATE_TWILIO_SIGNATURE=true.

    Args:
        agent: ID do agente (query param).

    Returns:
        TwiML vazio com status 200.
    """
    # Sanitização de campos do Twilio recebidos via x-www-form-urlencoded.
    # From vem como "whatsapp:+55...", WaId vem como "5511..." sem + (fallback).
    phone_number = (from_number or "").replace("whatsapp:", "")
    if not phone_number and wa_id:
        # WaId pode vir sem + — normaliza para E.164
        phone_number = wa_id if wa_id.startswith("+") else f"+{wa_id}"
    body = body or ""
    to_number = (to_number_form or "").replace("whatsapp:", "")
    message_sid = message_sid or ""

    # Rejeita webhook sem identidade de remetente (From e WaId ambos vazios)
    if not phone_number:
        logger.warning(
            "webhook_missing_sender",
            message_sid=message_sid,
            from_raw=from_number,
            wa_id_raw=wa_id,
        )
        raise HTTPException(
            status_code=400,
            detail="Missing sender identity (From/WaId)",
        )

    # Opt-out deve ser aceito antes de rate limit, debounce e agente. A resposta
    # é estática e abre apenas a confirmação solicitada pelo próprio usuário.
    if is_opt_out_command(body):
        pool = await get_pool()
        await record_whatsapp_opt_out(pool, phone_number, message_sid)
        logger.info("whatsapp_opt_out_recorded", message_id=message_sid)
        return Response(content=OPT_OUT_TWIML, media_type="application/xml")

    # Números usados em campanhas não funcionam como canal conversacional.
    # O opt-out acima continua operacional, mas nenhuma outra resposta pode
    # alcançar rate limit, fila, worker ou agente.
    if mode == "campaign":
        logger.info(
            "whatsapp_campaign_reply_ignored",
            phone=phone_number,
            message_id=message_sid,
        )
        return Response(content=EMPTY_TWIML, media_type="application/xml")

    # O agente só é relevante no modo conversacional.
    available_agents = list_agents()
    if agent not in available_agents:
        raise AgentNotFoundError(agent)

    # Mídia (imagem, áudio)
    try:
        num_media = int(num_media_raw or "0")
    except ValueError:
        num_media = 0
    media_url = media_url_form.strip() if (num_media > 0 and media_url_form) else None
    media_type = (
        media_type_form.strip() if (num_media > 0 and media_type_form) else None
    )

    # Rate limit
    await check_rate_limit(phone_number)

    # Enfileira a mensagem
    pool = await get_pool()
    result = await enqueue_or_buffer(
        pool=pool,
        phone_number=phone_number,
        agent_id=agent,
        body=body,
        media_url=media_url,
        media_type=media_type,
        to_number=to_number,
        message_id=message_sid,
        buffer_seconds=settings.message_buffer_seconds,
    )

    logger.info(
        "webhook_twilio_received",
        phone=phone_number,
        agent_id=agent,
        message_id=result.message_id,
        buffered=result.is_buffered,
    )

    return Response(content=EMPTY_TWIML, media_type="application/xml")
