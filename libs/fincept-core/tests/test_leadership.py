import pytest

from fincept_core.leadership import Leader


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def eval(
        self, script: str, numkeys: int, key: str, token: str, ttl: int | None = None
    ) -> int:
        if "pexpire" in script:
            return int(self.store.get(key) == token)
        if "del" in script:
            if self.store.get(key) == token:
                del self.store[key]
                return 1
            return 0
        return 0


@pytest.mark.asyncio
async def test_only_one_leader_acquires_shared_lock():
    redis = FakeRedis()
    a = Leader(redis=redis, role="test", ttl_seconds=5)  # type: ignore[arg-type]
    b = Leader(redis=redis, role="test", ttl_seconds=5)  # type: ignore[arg-type]
    await a._step()
    await b._step()
    assert a.is_leader ^ b.is_leader
    await a.stop()
    await b.stop()
