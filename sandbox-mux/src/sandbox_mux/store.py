"""Redis state shared by all replicas of the session manager.

Every decision that must hold across replicas (which sandbox a user is bound to, how
many users a sandbox has, how many sandboxes exist or are being created) is made inside
a Lua script, so it is atomic. Python never does read-then-write on those keys.

Keys (all under `{prefix}:{pool}:`):
  load              zset  sandbox_id -> number of users bound to it
  state             hash  sandbox_id -> ready | draining
  meta              hash  sandbox_id -> JSON (created time)
  sbxactive         zset  sandbox_id -> last turn time (ms)
  creating          zset  reservation token -> expiry (ms); one per in-flight create
  waiting           zset  user id -> expiry (ms); users waiting for capacity
  useractive        zset  user id -> last turn time (ms)
  user:{uid}        hash  sandbox, bound_at, provisioned
  lock:user:{uid}   string  turn lock (token, PX)
  leader            string  reaper leader lock
  hist:{uid}:{conv} list  JSON messages
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from typing import Literal

import redis.asyncio as redis

# KEYS: 1 user hash, 2 load, 3 state, 4 creating, 5 useractive, 6 waiting
# ARGV: uid, now_ms, cap, max_sandboxes, strategy, reservation_ttl_ms, token, waiting_ttl_ms
ASSIGN = """
local uid, now = ARGV[1], tonumber(ARGV[2])
local cap, maxs = tonumber(ARGV[3]), tonumber(ARGV[4])
local strat, rttl, token = ARGV[5], tonumber(ARGV[6]), ARGV[7]
local wttl = tonumber(ARGV[8])

local cur = redis.call('HGET', KEYS[1], 'sandbox')
if cur then
  if redis.call('HGET', KEYS[3], cur) == 'ready' then
    redis.call('ZADD', KEYS[5], now, uid)
    redis.call('ZREM', KEYS[6], uid)
    return {'bound', cur, '0'}
  end
  -- Stale binding: the sandbox is gone or draining. Drop it and place the user again.
  redis.call('DEL', KEYS[1])
  local sc = redis.call('ZSCORE', KEYS[2], cur)
  if sc and tonumber(sc) > 0 then redis.call('ZINCRBY', KEYS[2], -1, cur) end
end

local best, bestscore = nil, nil
local items = redis.call('ZRANGE', KEYS[2], 0, -1, 'WITHSCORES')
for i = 1, #items, 2 do
  local sid, sc = items[i], tonumber(items[i + 1])
  if sc < cap and redis.call('HGET', KEYS[3], sid) == 'ready' then
    if best == nil or (strat == 'pack' and sc > bestscore) or (strat == 'spread' and sc < bestscore) then
      best, bestscore = sid, sc
    end
  end
end
if best then
  redis.call('ZINCRBY', KEYS[2], 1, best)
  redis.call('HSET', KEYS[1], 'sandbox', best, 'bound_at', now)
  redis.call('ZADD', KEYS[5], now, uid)
  redis.call('ZREM', KEYS[6], uid)
  return {'bound', best, '1'}
end

-- No free slot. Register as a waiter, then decide whether a new sandbox is justified:
-- only if the waiters exceed what the sandboxes already being created can absorb.
redis.call('ZREMRANGEBYSCORE', KEYS[6], '-inf', now)
redis.call('ZADD', KEYS[6], now + wttl, uid)
redis.call('ZREMRANGEBYSCORE', KEYS[4], '-inf', now)
local waiters = redis.call('ZCARD', KEYS[6])
local inflight = redis.call('ZCARD', KEYS[4])
local total = redis.call('HLEN', KEYS[3]) + inflight
if total < maxs and inflight * cap < waiters then
  redis.call('ZADD', KEYS[4], now + rttl, token)
  return {'create', token, '0'}
end
if total >= maxs and inflight == 0 then return {'full', '', '0'} end
return {'wait', '', '0'}
"""

# KEYS: 1 state, 2 creating, 3 load ; ARGV: now_ms, max_sandboxes, reservation_ttl_ms, token
# Unconditional (warm-pool) reservation: only the sandbox cap applies.
RESERVE = """
local now, maxs, rttl, token = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]), ARGV[4]
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
if redis.call('HLEN', KEYS[1]) + redis.call('ZCARD', KEYS[2]) >= maxs then return 0 end
redis.call('ZADD', KEYS[2], now + rttl, token)
return 1
"""

# KEYS: 1 load, 2 state, 3 creating, 4 meta, 5 sbxactive ; ARGV: sid, token, meta_json, now_ms
REGISTER = """
redis.call('ZADD', KEYS[1], 'NX', 0, ARGV[1])
redis.call('HSET', KEYS[2], ARGV[1], 'ready')
redis.call('ZREM', KEYS[3], ARGV[2])
redis.call('HSET', KEYS[4], ARGV[1], ARGV[3])
redis.call('ZADD', KEYS[5], ARGV[4], ARGV[1])
return 1
"""

# KEYS: 1 user hash, 2 load, 3 useractive ; ARGV: uid, sid
UNBIND = """
if redis.call('HGET', KEYS[1], 'sandbox') ~= ARGV[2] then return 0 end
redis.call('DEL', KEYS[1])
redis.call('ZREM', KEYS[3], ARGV[1])
local sc = redis.call('ZSCORE', KEYS[2], ARGV[2])
if sc and tonumber(sc) > 0 then redis.call('ZINCRBY', KEYS[2], -1, ARGV[2]) end
return 1
"""

# KEYS: 1 load, 2 state ; ARGV: sid. Succeeds only while the sandbox has no users.
DRAIN = """
local sc = redis.call('ZSCORE', KEYS[1], ARGV[1])
if sc and tonumber(sc) == 0 and redis.call('HGET', KEYS[2], ARGV[1]) == 'ready' then
  redis.call('HSET', KEYS[2], ARGV[1], 'draining')
  return 1
end
return 0
"""

# KEYS: 1 load, 2 state, 3 meta, 4 sbxactive ; ARGV: sid
FORGET = """
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('HDEL', KEYS[2], ARGV[1])
redis.call('HDEL', KEYS[3], ARGV[1])
redis.call('ZREM', KEYS[4], ARGV[1])
return 1
"""

RELEASE_LOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""

RENEW_LOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('PEXPIRE', KEYS[1], ARGV[2]) end
return 0
"""


@dataclass
class Assignment:
    kind: Literal["bound", "create", "wait", "full"]
    sandbox_id: str | None = None   # for bound
    token: str | None = None        # for create
    is_new_binding: bool = False


def now_ms() -> int:
    return int(time.time() * 1000)


class Store:
    def __init__(self, client: redis.Redis, prefix: str, pool: str) -> None:
        self.r = client
        self.p = f"{prefix}:{pool}:"
        self._assign = client.register_script(ASSIGN)
        self._reserve = client.register_script(RESERVE)
        self._register = client.register_script(REGISTER)
        self._unbind = client.register_script(UNBIND)
        self._drain = client.register_script(DRAIN)
        self._forget = client.register_script(FORGET)
        self._release = client.register_script(RELEASE_LOCK)
        self._renew = client.register_script(RENEW_LOCK)

    def k(self, name: str) -> str:
        return self.p + name

    # ---- placement -------------------------------------------------------------

    async def assign(self, uid: str, *, cap: int, max_sandboxes: int, strategy: str,
                     reservation_ttl_s: int, waiting_ttl_s: int) -> Assignment:
        token = secrets.token_hex(8)
        kind, a, b = await self._assign(
            keys=[self.k(f"user:{uid}"), self.k("load"), self.k("state"), self.k("creating"),
                  self.k("useractive"), self.k("waiting")],
            args=[uid, now_ms(), cap, max_sandboxes, strategy, reservation_ttl_s * 1000, token,
                  waiting_ttl_s * 1000],
        )
        kind, a, b = _s(kind), _s(a), _s(b)
        if kind == "bound":
            return Assignment("bound", sandbox_id=a, is_new_binding=b == "1")
        if kind == "create":
            return Assignment("create", token=a)
        return Assignment(kind)  # type: ignore[arg-type]

    async def stop_waiting(self, uid: str) -> None:
        await self.r.zrem(self.k("waiting"), uid)

    async def reserve_creation(self, *, max_sandboxes: int, reservation_ttl_s: int) -> str | None:
        token = secrets.token_hex(8)
        ok = await self._reserve(
            keys=[self.k("state"), self.k("creating"), self.k("load")],
            args=[now_ms(), max_sandboxes, reservation_ttl_s * 1000, token],
        )
        return token if int(ok) == 1 else None

    async def abort_creation(self, token: str) -> None:
        await self.r.zrem(self.k("creating"), token)

    async def register_sandbox(self, sandbox_id: str, token: str) -> None:
        await self._register(
            keys=[self.k("load"), self.k("state"), self.k("creating"), self.k("meta"),
                  self.k("sbxactive")],
            args=[sandbox_id, token, json.dumps({"created_ms": now_ms()}), now_ms()],
        )

    async def try_drain(self, sandbox_id: str) -> bool:
        return int(await self._drain(keys=[self.k("load"), self.k("state")],
                                     args=[sandbox_id])) == 1

    async def forget_sandbox(self, sandbox_id: str) -> None:
        await self._forget(keys=[self.k("load"), self.k("state"), self.k("meta"),
                                 self.k("sbxactive")], args=[sandbox_id])

    async def unbind(self, uid: str, sandbox_id: str) -> bool:
        return int(await self._unbind(
            keys=[self.k(f"user:{uid}"), self.k("load"), self.k("useractive")],
            args=[uid, sandbox_id])) == 1

    # ---- user and sandbox bookkeeping -------------------------------------------

    async def binding(self, uid: str) -> dict[str, str]:
        raw = await self.r.hgetall(self.k(f"user:{uid}"))
        return {_s(k): _s(v) for k, v in raw.items()}

    async def mark_provisioned(self, uid: str, sandbox_id: str) -> None:
        await self.r.hset(self.k(f"user:{uid}"), "provisioned", sandbox_id)

    async def touch(self, uid: str, sandbox_id: str) -> None:
        t = now_ms()
        async with self.r.pipeline(transaction=False) as p:
            p.zadd(self.k("useractive"), {uid: t}, xx=True)
            p.zadd(self.k("sbxactive"), {sandbox_id: t}, xx=True)
            await p.execute()

    async def idle_users(self, older_than_ms: int, limit: int = 200) -> list[str]:
        ids = await self.r.zrangebyscore(self.k("useractive"), "-inf", older_than_ms,
                                         start=0, num=limit)
        return [_s(i) for i in ids]

    async def loads(self) -> dict[str, int]:
        return {_s(k): int(v) for k, v in
                await self.r.zrange(self.k("load"), 0, -1, withscores=True)}

    async def states(self) -> dict[str, str]:
        return {_s(k): _s(v) for k, v in (await self.r.hgetall(self.k("state"))).items()}

    async def sandbox_last_active_ms(self, sandbox_id: str) -> int:
        s = await self.r.zscore(self.k("sbxactive"), sandbox_id)
        return int(s) if s is not None else 0

    async def creating_count(self) -> int:
        await self.r.zremrangebyscore(self.k("creating"), "-inf", now_ms())
        return int(await self.r.zcard(self.k("creating")))

    async def waiting_count(self) -> int:
        await self.r.zremrangebyscore(self.k("waiting"), "-inf", now_ms())
        return int(await self.r.zcard(self.k("waiting")))

    # ---- locks -----------------------------------------------------------------

    async def acquire_lock(self, name: str, ttl_s: float) -> str | None:
        token = secrets.token_hex(8)
        ok = await self.r.set(self.k(name), token, nx=True, px=int(ttl_s * 1000))
        return token if ok else None

    async def release_lock(self, name: str, token: str) -> None:
        await self._release(keys=[self.k(name)], args=[token])

    async def renew_lock(self, name: str, token: str, ttl_s: float) -> bool:
        return int(await self._renew(keys=[self.k(name)], args=[token, int(ttl_s * 1000)])) == 1

    # ---- conversation history ----------------------------------------------------

    def _hist(self, uid: str, conv: str) -> str:
        return self.k(f"hist:{uid}:{conv}")

    async def history(self, uid: str, conv: str, limit: int) -> list[dict]:
        raw = await self.r.lrange(self._hist(uid, conv), -limit, -1)
        return [json.loads(_s(x)) for x in raw]

    async def append_history(self, uid: str, conv: str, messages: list[dict],
                             ttl_s: int, keep: int) -> None:
        key = self._hist(uid, conv)
        async with self.r.pipeline(transaction=True) as p:
            p.rpush(key, *[json.dumps(m) for m in messages])
            p.ltrim(key, -keep, -1)
            p.expire(key, ttl_s)
            await p.execute()


def _s(v: object) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)
