from unittest.mock import AsyncMock, MagicMock

import pytest

from whatsapp_langchain.shared.opt_out import (
    is_opt_out_command,
    normalize_opt_out_command,
    record_whatsapp_opt_out,
)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("SAIR", True),
        (" sair ", True),
        ("Parar!", True),
        ("CANCELAR", True),
        ("Quero sair", False),
        ("Vou parar para pensar", False),
        ("", False),
    ],
)
def test_opt_out_commands_are_explicit(body, expected):
    assert is_opt_out_command(body) is expected


def test_normalize_opt_out_command_removes_accents_and_punctuation():
    assert normalize_opt_out_command("  remo-ver! ") == "REMOVER"


@pytest.mark.asyncio
async def test_record_opt_out_is_idempotent():
    connection = AsyncMock()
    context = AsyncMock()
    context.__aenter__.return_value = connection
    pool = MagicMock()
    pool.connection.return_value = context

    await record_whatsapp_opt_out(pool, "+5511999990000", "SM123")

    sql, params = connection.execute.await_args.args
    assert "ON CONFLICT (phone_number) DO UPDATE" in sql
    assert params == ("+5511999990000", "SM123")
    connection.commit.assert_awaited_once()
