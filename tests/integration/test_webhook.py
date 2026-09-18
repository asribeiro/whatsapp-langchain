"""Testes de integração do webhook — FastAPI TestClient.

Testa o fluxo de webhook sem banco de dados real.
Usa mocking para simular pool e operações de fila.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from whatsapp_langchain import __version__
from whatsapp_langchain.server.main import app

client = TestClient(app, raise_server_exceptions=False)
TEST_INTERNAL_SERVICE_TOKEN = "test-internal-token"


@pytest.fixture(autouse=True)
def mock_db(monkeypatch):
    """Mock do banco de dados para testes sem PostgreSQL."""
    from whatsapp_langchain.shared.config import settings

    mock_pool = AsyncMock()
    monkeypatch.setattr(settings, "internal_service_token", TEST_INTERNAL_SERVICE_TOKEN)

    with (
        patch(
            "whatsapp_langchain.server.routes.health.check_db_health",
            return_value=True,
        ),
        patch(
            "whatsapp_langchain.server.routes.webhook.get_pool",
            return_value=mock_pool,
        ),
        patch(
            "whatsapp_langchain.server.routes.admin.get_pool",
            return_value=mock_pool,
        ),
        patch("whatsapp_langchain.shared.db.get_pool", return_value=mock_pool),
        patch("whatsapp_langchain.shared.db.run_migrations"),
        patch("whatsapp_langchain.shared.db.close_pool"),
    ):
        yield mock_pool


class TestHealthCheck:
    """Testes do endpoint /health."""

    def test_health_ok(self):
        """Retorna 200 quando o banco está acessível."""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "database": "connected",
            "version": __version__,
        }


class TestWebhookSync:
    """Testes do webhook síncrono."""

    def test_sync_requires_agent(self):
        """Deve exigir o query param 'agent'."""
        response = client.post(
            "/webhook/sync",
            json={"phone": "+5511999999999", "message": "Olá"},
        )
        # Sem agent= -> 422 (query param obrigatório)
        assert response.status_code == 422

    def test_sync_nonexistent_agent(self):
        """Deve retornar erro para agente inexistente."""
        response = client.post(
            "/webhook/sync?agent=nao_existe",
            json={"phone": "+5511999999999", "message": "Olá"},
        )
        assert response.status_code == 400


class TestWebhookTwilio:
    """Testes do webhook Twilio."""

    def test_twilio_requires_agent(self):
        """Deve exigir o query param 'agent'."""
        response = client.post(
            "/webhook/twilio",
            data={
                "MessageSid": "SM123",
                "From": "whatsapp:+5511999999999",
                "To": "whatsapp:+14155238886",
                "Body": "Olá",
                "NumMedia": "0",
            },
        )
        # Sem agent= -> 422
        assert response.status_code == 422

    def test_twilio_nonexistent_agent(self):
        """Deve retornar erro para agente inexistente."""
        response = client.post(
            "/webhook/twilio?agent=nao_existe",
            data={
                "MessageSid": "SM123",
                "From": "whatsapp:+5511999999999",
                "To": "whatsapp:+14155238886",
                "Body": "Olá",
                "NumMedia": "0",
            },
        )
        assert response.status_code == 400

    @patch("whatsapp_langchain.server.routes.webhook.enqueue_or_buffer")
    def test_twilio_enqueues_message(self, mock_enqueue):
        """Deve enfileirar mensagem e retornar TwiML vazio."""
        from whatsapp_langchain.shared.models import EnqueueResult

        mock_enqueue.return_value = EnqueueResult(message_id=1, is_buffered=False)

        response = client.post(
            "/webhook/twilio?agent=rhawk_assistant",
            data={
                "MessageSid": "SM123",
                "From": "whatsapp:+5511999999999",
                "To": "whatsapp:+14155238886",
                "Body": "Olá",
                "NumMedia": "0",
            },
        )
        assert response.status_code == 200
        assert "Response" in response.text

    @patch("whatsapp_langchain.server.routes.webhook.enqueue_or_buffer")
    @patch("whatsapp_langchain.server.routes.webhook.record_whatsapp_opt_out")
    def test_twilio_intercepts_opt_out_before_agent(self, mock_record, mock_enqueue):
        """SAIR deve persistir supressão e nunca entrar na fila do agente."""
        response = client.post(
            "/webhook/twilio?agent=rhawk_assistant",
            data={
                "MessageSid": "SM123",
                "From": "whatsapp:+5511999999999",
                "To": "whatsapp:+55118596",
                "Body": "sair",
                "NumMedia": "0",
            },
        )
        assert response.status_code == 200
        assert "removido" in response.text
        mock_record.assert_awaited_once()
        mock_enqueue.assert_not_called()

    @patch("whatsapp_langchain.server.routes.webhook.check_rate_limit")
    @patch("whatsapp_langchain.server.routes.webhook.enqueue_or_buffer")
    @patch("whatsapp_langchain.server.routes.webhook.record_whatsapp_opt_out")
    def test_campaign_mode_ignores_non_opt_out_before_agent(
        self,
        mock_record,
        mock_enqueue,
        mock_rate_limit,
    ):
        """Campanha deve aceitar a resposta sem acionar a conversa."""
        response = client.post(
            "/webhook/twilio?agent=rhawk_assistant&mode=campaign",
            data={
                "MessageSid": "SM123",
                "From": "whatsapp:+5511999999999",
                "To": "whatsapp:+55118596",
                "Body": "Quero saber mais",
                "NumMedia": "0",
            },
        )
        assert response.status_code == 200
        assert response.text == (
            '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
        )
        mock_record.assert_not_called()
        mock_rate_limit.assert_not_called()
        mock_enqueue.assert_not_called()

    @patch("whatsapp_langchain.server.routes.webhook.enqueue_or_buffer")
    @patch("whatsapp_langchain.server.routes.webhook.record_whatsapp_opt_out")
    def test_campaign_mode_still_records_opt_out(self, mock_record, mock_enqueue):
        """SAIR continua sendo processado antes do bloqueio de conversa."""
        response = client.post(
            "/webhook/twilio?agent=rhawk_assistant&mode=campaign",
            data={
                "MessageSid": "SM124",
                "From": "whatsapp:+5511999999999",
                "To": "whatsapp:+55118596",
                "Body": "SAIR",
                "NumMedia": "0",
            },
        )
        assert response.status_code == 200
        assert "removido" in response.text
        mock_record.assert_awaited_once()
        mock_enqueue.assert_not_called()

    def test_twilio_openapi_exposes_form_fields(self):
        """Swagger deve exibir body form-encoded para teste manual."""
        openapi = client.get("/openapi.json").json()
        post = openapi["paths"]["/webhook/twilio"]["post"]
        assert "requestBody" in post

        form_content = post["requestBody"]["content"][
            "application/x-www-form-urlencoded"
        ]
        schema = form_content["schema"]
        if "$ref" in schema:
            ref_name = schema["$ref"].split("/")[-1]
            schema = openapi["components"]["schemas"][ref_name]

        properties = schema["properties"]
        assert "MessageSid" in properties
        assert "From" in properties
        assert "To" in properties
        assert "Body" in properties
        assert "NumMedia" in properties


class TestAdminRoutes:
    """Testes das rotas administrativas."""

    auth_headers = {"Authorization": f"Bearer {TEST_INTERNAL_SERVICE_TOKEN}"}

    def test_list_agents(self):
        """Deve listar agentes disponíveis."""
        response = client.get("/api/agents", headers=self.auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert "agents" in data
        assert "rhawk_assistant" in data["agents"]

    def test_list_agents_requires_token(self):
        """Deve rejeitar requisição sem token de serviço."""
        response = client.get("/api/agents")
        assert response.status_code == 401

    @patch("whatsapp_langchain.server.routes.admin.list_whatsapp_opt_outs")
    def test_whatsapp_suppressions_are_protected(self, mock_list):
        """Expõe o snapshot somente com autenticação administrativa."""
        mock_list.return_value = {
            "observedAt": "2026-09-18T20:00:00.000Z",
            "phones": ["+5511999990000"],
        }
        unauthorized = client.get("/api/whatsapp/suppressions")
        authorized = client.get(
            "/api/whatsapp/suppressions",
            headers=self.auth_headers,
        )
        assert unauthorized.status_code == 401
        assert authorized.status_code == 200
        assert authorized.json()["phones"] == ["+5511999990000"]
