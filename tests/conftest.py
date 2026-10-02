import os

import httpx
import pytest_asyncio

from app.config import Settings
from app.main import create_app

DATABASE_URL = os.getenv("TEST_DATABASE_URL")


@pytest_asyncio.fixture
async def client():
    if not DATABASE_URL:
        import pytest

        pytest.skip("TEST_DATABASE_URL is not set")
    settings = Settings(
        database_url=DATABASE_URL,
        auth_secret="test-auth-secret",
        admin_token="test-admin-token",
        db_pool_max=10,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=60) as http:
            yield http
