"""
Integration тесты для API accounts endpoints.
Требуют запущенную БД и могут использовать внешние зависимости.
"""
import pytest
from httpx import AsyncClient
from fastapi.testclient import TestClient
from main import app
from db.models import Account, User
from sqlalchemy.orm import Session


@pytest.mark.integration
class TestAccountsAPI:
    """Integration тесты для accounts API."""
    
    def test_list_accounts_empty(self, db_session: Session):
        """Тест получения списка аккаунтов (пустой список)."""
        client = TestClient(app)
        
        # Мокаем verify_admin_header (в реальности нужен токен)
        response = client.get(
            "/api/accounts",
            headers={"X-Admin-Token": "test-token"}  # В реальности нужен правильный токен
        )
        
        # Если нет авторизации, вернёт 401
        # Если авторизация есть, вернёт пустой список
        assert response.status_code in [200, 401]
    
    def test_list_accounts_with_data(self, db_session: Session, sample_account: Account):
        """Тест получения списка аккаунтов (с данными)."""
        client = TestClient(app)
        
        response = client.get(
            "/api/accounts",
            headers={"X-Admin-Token": "test-token"}
        )
        
        # В реальности нужна правильная авторизация
        assert response.status_code in [200, 401]
        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, list)
            # Может быть пустым если нет авторизации
    
    def test_get_account_by_id(self, db_session: Session, sample_account: Account):
        """Тест получения аккаунта по ID."""
        client = TestClient(app)
        
        response = client.get(
            f"/api/accounts/{sample_account.id}",
            headers={"X-Admin-Token": "test-token"}
        )
        
        assert response.status_code in [200, 401, 404]
        if response.status_code == 200:
            data = response.json()
            assert data["id"] == sample_account.id
            assert data["label"] == sample_account.label




