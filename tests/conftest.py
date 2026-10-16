import pytest_asyncio

from canaldark.db import open_db


@pytest_asyncio.fixture
async def db(tmp_path):
    conn = await open_db(tmp_path / "t.db")
    yield conn
    await conn.close()
