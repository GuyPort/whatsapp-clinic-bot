"""Deterministic test doubles shared by conversation-state tests."""

from datetime import datetime, timedelta, timezone
from threading import Condition, RLock, current_thread
from copy import deepcopy
import json


class ManualClock:
    def __init__(self, value: datetime):
        self._lock = RLock()
        self.set(value)

    def now(self) -> datetime:
        with self._lock:
            return self._value

    def set(self, value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ManualClock requires an aware datetime")
        with self._lock:
            self._value = value

    def advance(self, delta: timedelta) -> datetime:
        if delta < timedelta(0):
            raise ValueError("ManualClock cannot move backwards")
        with self._lock:
            self._value += delta
            return self._value


class ScriptRedis:
    """In-memory Redis command semantics for the store's atomic command plan.

    This deliberately simulates the dependency, never the store's manifest logic.
    No sockets, persistent data, imported Redis client, or configuration lookup.
    """
    def __init__(self, clock, config):
        from app.conversation_redis import GLOBAL_EPOCH_KEY
        self.clock, self.lock = clock, RLock()
        self.values = {GLOBAL_EPOCH_KEY: str(config.coordination_epoch)}
        self.sets, self.expiry = {}, {}
        self.server = {"run_id": config.redis_expected_run_id}
        self.memory = {"maxmemory_policy": "noeviction"}
        self.persistence = {"aof_enabled": 1, "aof_last_write_status": "ok", "loading": 0}
        self.fail_operation = None
        self.before_atomic = None
        self.global_epoch_writes = 0
        self.denied_commands = set()
        self.acl_check_available = True

    def _acl_command(self, command, key):
        if (command, key) in self.denied_commands:
            raise PermissionError("NOPERM")

    def _expire(self):
        for key, deadline in list(self.expiry.items()):
            if self.clock.now().timestamp() >= deadline:
                self.values.pop(key, None)
                self.sets.pop(key, None)
                del self.expiry[key]

    def ping(self):
        return True

    def info(self, section):
        return dict(getattr(self, section))

    def get(self, key):
        with self.lock:
            self._expire()
            if key in self.sets:
                raise TypeError("WRONGTYPE")
            return self.values.get(key)

    def sismember(self, key, member):
        with self.lock:
            self._expire()
            if key in self.values:
                raise TypeError("WRONGTYPE")
            return int(member in self.sets.get(key, set()))

    def scan_iter(self, match):
        with self.lock:
            self._expire()
            return iter(key for key in (*self.values, *self.sets) if key.startswith(match[:-1]))

    def sscan_iter(self, key, match):
        with self.lock:
            return iter(member for member in self.sets.get(key, set()) if member.startswith(match[:-1]))

    def eval(self, script, count, *args):
        from app.conversation_redis import ATOMIC_SCRIPT
        if script != ATOMIC_SCRIPT:
            raise ValueError("unsupported script")
        keys, plan = args[:count], json.loads(args[count])
        def key(item):
            return keys[item["key"] - 1]
        with self.lock:
            if not self.acl_check_available:
                return "unavailable"
            if self.before_atomic is not None:
                hook, self.before_atomic = self.before_atomic, None
                hook()
            self._expire()
            if self.fail_operation == plan["operation"]:
                self.fail_operation = None
                raise RuntimeError("injected atomic failure")
            if (self.get(keys[plan["epoch_key"] - 1]) != plan["epoch"]
                    or self.server["run_id"] != plan["run_id"]
                    or self.memory["maxmemory_policy"] != "noeviction"
                    or self.persistence["aof_enabled"] != 1
                    or self.persistence["aof_last_write_status"] != "ok"
                    or self.persistence["loading"] != 0):
                return "readiness"
            for check in plan["checks"]:
                if ((check["op"] == "get" and key(check) in self.sets)
                        or (check["op"] != "get" and key(check) in self.values)):
                    return "unavailable"
            # Same contract as Lua: validate the entire batch, then authorize all
            # normal and possible quarantine writes, before touching any value.
            simulated_types = {}
            for write in plan["writes"]:
                if (type(write.get("key")) is not int or not 1 <= write["key"] <= len(keys)
                        or write.get("op") not in ("SET", "ACQUIRE", "PEXPIRE", "DEL", "SADD", "SREM")):
                    return "unavailable"
                target, op = key(write), write["op"]
                if op not in ("DEL", "PEXPIRE") and not isinstance(write.get("value"), str):
                    return "unavailable"
                if op in ("ACQUIRE", "PEXPIRE") or "ttl" in write:
                    ttl = write.get("ttl")
                    if type(ttl) is not int or not 1 <= ttl <= 9007199254740991:
                        return "unavailable"
                if op == "ACQUIRE" and len(plan["writes"]) != 1:
                    return "unavailable"
                kind = simulated_types.get(target, "string" if target in self.values else
                                           "set" if target in self.sets else "none")
                if op in ("SADD", "SREM") and kind not in ("none", "set"):
                    return "unavailable"
                if op in ("SET", "ACQUIRE"):
                    simulated_types[target] = "string"
                elif op == "DEL":
                    simulated_types[target] = "none"
                elif op == "SADD":
                    simulated_types[target] = "set"
                self._acl_command("SET" if op == "ACQUIRE" else op, target)
            self._acl_command("SET", keys[plan["anchor_key"] - 1])
            self._acl_command("SADD", keys[plan["quarantine_key"] - 1])
            if keys[plan["quarantine_key"] - 1] in self.values:
                return "unavailable"
            def quarantine():
                self._acl_command("SET", keys[plan["anchor_key"] - 1])
                self.values[keys[plan["anchor_key"] - 1]] = '{"cycle":"QUARANTINED"}'
                self._acl_command("SADD", keys[plan["quarantine_key"] - 1])
                self.sets.setdefault(keys[plan["quarantine_key"] - 1], set()).add(plan["digest"])
                return "generation"
            for check in plan["checks"]:
                actual = (self.get(key(check)) if check["op"] == "get"
                          else self.sismember(key(check), check["member"]))
                if actual != check["value"]:
                    return quarantine() if check["failure"] == "generation" else check["failure"]
            if plan["quarantine"]:
                return quarantine()
            for write in plan["writes"]:
                target, op = key(write), write["op"]
                self._acl_command("SET" if op == "ACQUIRE" else op, target)
                if op in ("SET", "ACQUIRE"):
                    if op == "ACQUIRE" and (target in self.values or target in self.sets):
                        return "locked"
                    self.sets.pop(target, None)
                    self.values[target] = write["value"]
                    self.expiry.pop(target, None)
                    if "ttl" in write:
                        self.expiry[target] = self.clock.now().timestamp() + write["ttl"] / 1000
                    if target == keys[plan["epoch_key"] - 1]:
                        self.global_epoch_writes += 1
                elif op == "DEL":
                    self.values.pop(target, None)
                    self.sets.pop(target, None)
                    self.expiry.pop(target, None)
                elif op == "PEXPIRE":
                    if target in self.values or target in self.sets:
                        self.expiry[target] = self.clock.now().timestamp() + write["ttl"] / 1000
                elif op == "SADD":
                    self.sets.setdefault(target, set()).add(write["value"])
                elif op == "SREM":
                    self.sets.get(target, set()).discard(write["value"])
                    if not self.sets.get(target):
                        self.sets.pop(target, None)
                else:
                    raise ValueError("unsupported operation")
            return "ok"


from app.conversation_redis import RedisConversationStore


class InMemoryConversationStore(RedisConversationStore):
    """Real store algorithms over local, deterministic atomic Redis semantics."""
    def __init__(self, config):
        clock = ManualClock(datetime(2026, 9, 12, 12, tzinfo=timezone.utc))
        super().__init__(ScriptRedis(clock, config), config, clock)

    @property
    def global_epoch_writes(self):
        return self.client.global_epoch_writes

    @property
    def before_atomic(self):
        return self.client.before_atomic

    @before_atomic.setter
    def before_atomic(self, value):
        self.client.before_atomic = value

    def fail_next_atomic(self, operation_name):
        self.client.fail_operation = operation_name

    def inject_fault(self, fault):
        from app.conversation_redis import GLOBAL_EPOCH_KEY
        if fault == "epoch_absent":
            self.delete_global_epoch()
        elif fault == "epoch_mismatch":
            self.client.values[GLOBAL_EPOCH_KEY] = "mismatch"
        elif fault == "run_id_mismatch":
            self.client.server["run_id"] = "mismatch"
        elif fault == "noeviction_invalid":
            self.client.memory["maxmemory_policy"] = "allkeys-lru"
        elif fault == "persistence_invalid":
            self.client.persistence["aof_enabled"] = 0
        else:
            raise ValueError("unknown fault")

    def delete_global_epoch(self):
        from app.conversation_redis import GLOBAL_EPOCH_KEY
        self.client.values.pop(GLOBAL_EPOCH_KEY, None)

    def snapshot(self):
        with self.client.lock:
            return deepcopy((self.client.values, self.client.sets, self.client.expiry))

    def contact_snapshot(self, phone):
        from app.conversation_redis import contact_keys
        keys = contact_keys(phone)
        with self.client.lock:
            prefix = keys.anchor.rsplit(":", 1)[0] + ":"
            values = {key: value for key, value in self.client.values.items()
                      if key.startswith(prefix) and key != keys.lease}
            anchor = json.loads(values.get(keys.anchor, "{}"))
            return {"revision": anchor.get("contact_revision"), "values": values}

    def delete_detail(self, phone, entry):
        self.client.values.pop(self._detail_key(phone, entry), None)

    def is_quarantined(self, phone):
        from app.conversation_redis import contact_keys, contact_digest, QUARANTINE_INDEX_KEY
        raw = self.client.get(contact_keys(phone).anchor)
        return (raw is not None and json.loads(raw)["cycle"] == "QUARANTINED"
                and self.client.sismember(QUARANTINE_INDEX_KEY, contact_digest(phone)))

    def corrupt_contact(self, phone, fault):
        from app.conversation_redis import contact_keys, INDEX_KEYS
        keys = contact_keys(phone)
        if fault in INDEX_KEYS:
            self.client.sets.pop(INDEX_KEYS[fault], None)
        elif fault == "generation":
            self.client.values[keys.generation] = "divergent"
        else:
            anchor = json.loads(self.client.values[keys.anchor])
            if fault == "fingerprint":
                anchor["manifest_fingerprint"] = "bad"
            elif fault == "manifest":
                del anchor["manifest"]
            self.client.values[keys.anchor] = json.dumps(anchor)

    def lease_remaining(self, phone):
        from app.conversation_redis import contact_keys
        return self.client.expiry[contact_keys(phone).lease] - self.clock.now().timestamp()


class ControlledWait:
    """Wake the daemon only when its requested interval has elapsed in manual time."""
    def __init__(self, clock):
        self.clock, self.condition = clock, Condition()
        self.requests, self.ticks = 0, 0
        self.acknowledged = 0
        self.deadline = None
        self.worker = None
        self.stopped = False

    def __call__(self, event, interval):
        with self.condition:
            self.worker = current_thread()
            self.requests += 1
            self.deadline = self.clock.now() + timedelta(seconds=interval)
            self.condition.notify_all()
            while self.clock.now() < self.deadline and not event.is_set():
                self.acknowledged = self.ticks
                self.condition.notify_all()
                self.condition.wait(timeout=0.01)
            self.stopped = event.is_set()
            return self.stopped

    def tick(self, seconds):
        with self.condition:
            assert self.condition.wait_for(lambda: self.deadline is not None, timeout=2)
            self.clock.advance(timedelta(seconds=seconds))
            self.ticks += 1
            tick = self.ticks
            request = self.requests
            due = self.clock.now() >= self.deadline
            self.condition.notify_all()
        # An unsuccessful renewal exits the daemon, so there is no next wait call.
        for _ in range(200):
            with self.condition:
                completed = self.requests > request if due else self.acknowledged >= tick
                if completed or not self.worker.is_alive():
                    self.stopped = not self.worker.is_alive()
                    return
                self.condition.wait(timeout=0.01)
        raise AssertionError("heartbeat did not finish its controlled iteration")
