import fakeredis
import fakeredis.aioredis
import pytest

from sandbox_mux.backends.fake import FakeSandboxBackend
from sandbox_mux.config import Config
from sandbox_mux.pool import PoolManager
from sandbox_mux.reaper import Reaper
from sandbox_mux.service import SessionService
from sandbox_mux.store import Store


def make_cfg(**pool) -> Config:
    return Config.model_validate({
        "backend": "fake",
        "pool": {"name": "t", "max_sandboxes": 4, "users_per_sandbox": 3, **pool},
        "session": {"queue_timeout_seconds": 2, "user_idle_ttl_seconds": 60},
        "auth": {"mode": "dev"},
    })


class Replica:
    """One session-manager process. Several replicas share one Redis and one backend."""

    def __init__(self, cfg, server, backend):
        self.cfg, self.backend = cfg, backend
        self.redis = fakeredis.aioredis.FakeRedis(server=server)
        self.store = Store(self.redis, "smux", cfg.pool.name)
        self.pool = PoolManager(cfg, self.store, backend)
        self.service = SessionService(cfg, self.store, backend, self.pool)
        self.reaper = Reaper(cfg, self.store, backend, self.pool, self.service)


@pytest.fixture
def server():
    return fakeredis.FakeServer()


@pytest.fixture
def backend():
    return FakeSandboxBackend(create_delay=0.05)


@pytest.fixture
def make_replica(server, backend):
    def _make(**pool):
        return Replica(make_cfg(**pool), server, backend)
    return _make
