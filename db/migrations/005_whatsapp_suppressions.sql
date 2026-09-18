-- 005_whatsapp_suppressions.sql
-- Supressões explícitas do canal WhatsApp. O telefone é necessário para
-- impedir novos contatos e só é exposto por endpoint administrativo protegido.

CREATE TABLE whatsapp_suppressions (
    phone_number  TEXT PRIMARY KEY,
    reason        TEXT NOT NULL,
    source        TEXT NOT NULL,
    message_id    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT whatsapp_suppressions_phone_e164
        CHECK (phone_number ~ '^\+[1-9][0-9]{7,14}$')
);

-- Preserva comandos explícitos que possam ter chegado antes desta migração.
-- DISTINCT ON escolhe a ocorrência mais recente sem duplicar por telefone.
INSERT INTO whatsapp_suppressions
    (phone_number, reason, source, message_id, created_at, updated_at)
SELECT DISTINCT ON (phone_number)
    phone_number,
    'user_opt_out',
    'historical_inbound_keyword',
    message_id,
    created_at,
    updated_at
FROM message_queue
WHERE phone_number ~ '^\+[1-9][0-9]{7,14}$'
  AND regexp_replace(
      upper(trim(incoming_message)),
      '[^A-Z]',
      '',
      'g'
  ) IN ('CANCEL', 'CANCELAR', 'PARAR', 'REMOVER', 'SAIR', 'STOP', 'UNSUBSCRIBE')
ORDER BY phone_number, created_at DESC;

CREATE INDEX idx_whatsapp_suppressions_updated
    ON whatsapp_suppressions (updated_at DESC);
