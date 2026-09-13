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
        self.before_operation = {}
        self.after_operation = {}
        self.operation_calls = {}
        self.write_counts = {}
        self.fail_write_at = None
        self.sscan_calls = []
        self.sscan_chunk_limit = None

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

    def sscan(self, key, cursor=0, count=100):
        """Deterministic scan pages; a COUNT-sized chunk may exceed output limit."""
        with self.lock:
            self._expire()
            self.sscan_calls.append((key, cursor, count))
            if key in self.values:
                raise TypeError("WRONGTYPE")
            members = sorted(self.sets.get(key, set()))
            start = int(cursor)
            if self.sscan_chunk_limit is not None:
                count = min(count, self.sscan_chunk_limit)
            stop = min(start + count, len(members))
            return (0 if stop >= len(members) else stop), members[start:stop]

    def eval(self, script, count, *args):
        from app.conversation_redis import (ATOMIC_SCRIPT, EPOCH_ROTATION_SCRIPT, GLOBAL_EPOCH_KEY,
                                            RECOVERY_CHECKPOINT_SCRIPT, RECOVERY_CHECKPOINT_KEY)
        if script == RECOVERY_CHECKPOINT_SCRIPT:
            with self.lock:
                if self.before_atomic is not None:
                    hook, self.before_atomic = self.before_atomic, None
                    hook()
                if count != 2 or len(args) != 6 or args[:2] != (GLOBAL_EPOCH_KEY, RECOVERY_CHECKPOINT_KEY) or not self.acl_check_available:
                    return "failed"
                epoch_key, checkpoint_key, epoch, run_id, expected, new = args
                for target in (epoch_key, checkpoint_key):
                    self._acl_command("GET", target)
                    self._acl_command("TYPE", target)
                    if target in self.sets:
                        return "failed"
                for section in ("server", "memory", "persistence"):
                    self._acl_command("INFO", section)
                self._acl_command("SET", checkpoint_key)
                if (self.get(epoch_key) != epoch or self.server["run_id"] != run_id
                        or self.memory["maxmemory_policy"] != "noeviction"
                        or self.persistence != {"aof_enabled": 1, "aof_last_write_status": "ok", "loading": 0}
                        or (self.get(checkpoint_key) or "") != expected):
                    return "failed"
                self.values[checkpoint_key] = new
                self.expiry.pop(checkpoint_key, None)
                return "ok"
        if script == EPOCH_ROTATION_SCRIPT:
            with self.lock:
                if self.before_atomic is not None:
                    hook, self.before_atomic = self.before_atomic, None
                    hook()
                if count != 1 or len(args) != 4 or args[0] != GLOBAL_EPOCH_KEY or not self.acl_check_available:
                    return "failed"
                key, expected, new, run_id = args
                for command, target in (("GET", key), ("TYPE", key), ("SET", key),
                        ("INFO", "server"), ("INFO", "memory"), ("INFO", "persistence")):
                    self._acl_command(command, target)
                if (key in self.sets or self.get(key) != expected or expected == new
                        or self.server["run_id"] != run_id or self.memory["maxmemory_policy"] != "noeviction"
                        or self.persistence != {"aof_enabled": 1, "aof_last_write_status": "ok", "loading": 0}):
                    return "failed"
                self.values[key] = new
                self.expiry.pop(key, None)
                self.global_epoch_writes += 1
                return "ok"
        if script != ATOMIC_SCRIPT:
            raise ValueError("unsupported script")
        keys, plan = args[:count], json.loads(args[count])
        def key(item):
            return keys[item["key"] - 1]
        with self.lock:
            self.operation_calls[plan["operation"]] = self.operation_calls.get(plan["operation"], 0) + 1
            if not self.acl_check_available:
                return "unavailable"
            operation_hook = self.before_operation.pop(plan["operation"], None)
            if operation_hook is not None:
                operation_hook()
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
            self.write_counts[plan["operation"]] = len(plan["writes"])
            for index, write in enumerate(plan["writes"] + plan.get("deadline_writes", []) + plan.get("quarantine_writes", [])):
                # Dependency fault at each planned write is discovered in the
                # same ACL preflight the real Lua executes before its first write.
                if self.fail_write_at == (plan["operation"], index):
                    self.fail_write_at = None
                    raise PermissionError("NOPERM")
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
                for write in plan.get("quarantine_writes", []):
                    self.values.pop(key(write), None)
                    self.expiry.pop(key(write), None)
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
            expired = "deadline_us" in plan and int(self.clock.now().timestamp() * 1000000) >= plan["deadline_us"]
            selected_writes = plan.get("deadline_writes", []) if expired else plan["writes"]
            for write in selected_writes:
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
            operation_hook = self.after_operation.pop(plan["operation"], None)
            if operation_hook is not None:
                operation_hook()
            return "pending" if expired else "ok"


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


class BarrierSession:
    """Real in-memory SQL session with deterministic transaction boundaries."""
    def __init__(self, session):
        assert session.get_bind().url.database in (None, "", ":memory:")
        self.session = session
        self.hooks = {}
        self.events = []

    def __getattr__(self, name):
        return getattr(self.session, name)

    def _at(self, boundary):
        self.events.append(boundary)
        hook = self.hooks.pop(boundary, None)
        if hook is not None:
            hook()

    def flush(self):
        self._at("flush_entered")
        self.session.flush()
        self._at("flush")

    def add(self, instance, *args, **kwargs):
        self.session.add(instance, *args, **kwargs)
        self._at("add_returned")

    def execute(self, statement, *args, **kwargs):
        if getattr(statement, "is_delete", False) and statement.table.name == "conversation_contexts":
            self._at("before_context_delete")
        if getattr(statement, "is_delete", False) and statement.table.name == "appointments":
            self._at("before_appointment_delete")
        self._at("execute")
        return self.session.execute(statement, *args, **kwargs)

    def commit(self):
        self._at("commit_entered")
        self.session.commit()
        self._at("commit_returned")

    def rollback(self):
        self.session.rollback()
        self._at("rollback")


class ScriptedBroker:
    """No network: typed confirmation, definitive failure or ambiguous outcome."""
    def __init__(self):
        self.calls = []
        self.next_result = None
        self.on_enqueue = None

    def probe(self):
        return True

    def enqueue_processing(self, command):
        from app.conversation_state import EnqueueResult
        self.calls.append(command)
        if self.on_enqueue:
            self.on_enqueue(command)
        return self.next_result or EnqueueResult.CONFIRMED


class ScriptedClaude:
    """Complete SDK-shaped responses without constructing an HTTP client."""

    def __init__(self):
        self.messages = self
        self.calls = []
        self.responses = []
        self.on_create = None

    def respond(self, *blocks, stop_reason=None):
        from anthropic.types import Message, Usage

        self.responses.append(Message(
            id="msg_synthetic",
            type="message",
            role="assistant",
            model="claude-sonnet-4-6",
            content=list(blocks),
            stop_reason=stop_reason or (
                "tool_use" if any(block.type == "tool_use" for block in blocks) else "end_turn"
            ),
            stop_sequence=None,
            usage=Usage(input_tokens=1, output_tokens=1),
        ))

    def respond_with_text(self, text):
        from anthropic.types import TextBlock

        self.respond(TextBlock(type="text", text=text))

    def respond_with_tool(self, name, tool_input=None, tool_id="tool_synthetic"):
        from anthropic.types import ToolUseBlock

        self.respond(ToolUseBlock(
            type="tool_use", id=tool_id, name=name,
            input={} if tool_input is None else tool_input,
        ))

    def create(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        if self.on_create is not None:
            self.on_create(kwargs)
        if not self.responses:
            raise AssertionError("unexpected Claude invocation")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ForbiddenAgentEffects:
    """Tripwire for effects outside the pure agent's injected model boundary."""

    def __init__(self):
        self.calls = []

    def boundary(self, name):
        def forbidden(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError("forbidden agent effect")
        return forbidden


class WebhookRequest:
    """Header/body separation detects premature JSON access."""
    def __init__(self, app, payload=None, *, signature="synthetic-webhook-secret", json_error=None):
        from starlette.datastructures import Headers
        self.app = app
        self.headers = Headers({} if signature is None else {"X-Webhook-Signature": signature})
        self.payload, self.json_error = payload, json_error
        self.json_calls = 0

    async def json(self):
        self.json_calls += 1
        if self.json_error is not None:
            raise self.json_error
        return self.payload


def webhook_payload(*, jid="5551999990000@s.whatsapp.net", text="Mensagem sintética",
                    from_me=False, message_id="synthetic-message-id", media=None,
                    key_fields=None, nested=True):
    key = {"remoteJid": jid, "fromMe": from_me, "id": message_id, **(key_fields or {})}
    message = {"conversation": text} if media is None else {media: {"url": "synthetic-media-url"}}
    data = {"key": key, "message": message, "messageTimestamp": 0, "pushName": "Synthetic"}
    return {"event": "messages.upsert", "data": {"messages": data} if nested else data}


class IngressRuntime:
    """Real coordinator/store with in-memory SQL and scripted external boundary."""
    def __init__(self, factory, config):
        from app.conversation_state import ConversationCoordinator, DependencyName
        self.store = InMemoryConversationStore(config)
        self.clock = self.store.clock
        from app.conversation_tasks import _require_ready
        self.coordinator = ConversationCoordinator(self.store, self.clock, require_ready=lambda: _require_ready(self))
        self.processing_broker = ScriptedBroker()
        self._factory = factory
        self.dependencies = {name: True for name in DependencyName}
        self.readiness_calls = self.session_calls = 0

    @property
    def lease_calls(self):
        return self.store.client.operation_calls.get("acquire", 0)

    def readiness_status(self):
        from app.conversation_state import DependencyStatus, ReadinessReport
        self.readiness_calls += 1
        return ReadinessReport(tuple(DependencyStatus(name, ready) for name, ready in self.dependencies.items()))

    def session_factory(self):
        self.session_calls += 1
        session = self._factory()
        assert session.bind.url.database in (None, "", ":memory:")
        return session

    def details(self, phone="5551999990000"):
        with self.store.contact_lease(phone) as lease:
            return self.store.read_details(lease)

    def envelopes(self, phone="5551999990000"):
        return [envelope for detail in self.details(phone) if detail.entry.kind in ("buffer", "staging")
                for envelope in detail.body.get("envelopes", [])]

    def pause(self, phone="5551999990000"):
        from uuid import uuid4
        with self.store.contact_lease(phone) as lease, self._factory() as db:
            return self.coordinator.pause_for_secretary(
                db, phone, "secretary_manual_pause", self.clock.now(), lease, str(uuid4()))


class ScriptedAgent:
    """Typed agent boundary; the real coordinator still owns all SQL effects."""
    def __init__(self):
        self.calls = []
        self.on_prepare = None
        self.intent = None

    def prepare_result(self, message, phone, snapshot):
        from app.conversation_state import AgentResult, AgentIntent
        self.calls.append((message, phone, deepcopy(snapshot)))
        if self.on_prepare:
            self.on_prepare()
        return AgentResult("Resposta sintética", snapshot.messages + [
            {"role": "user", "content": message},
            {"role": "assistant", "content": "Resposta sintética"}],
            snapshot.current_flow, deepcopy(snapshot.flow_data), self.intent or AgentIntent.SAVE_CONTEXT)


class ScriptedOutboundBroker:
    def __init__(self):
        self.calls = []
        self.on_enqueue = None
        self.next_result = None

    def enqueue_outbound(self, outbound):
        from app.conversation_state import EnqueueResult
        self.calls.append(outbound)
        if self.on_enqueue:
            self.on_enqueue(outbound)
        return self.next_result or EnqueueResult.CONFIRMED


class ScriptedTransport:
    def __init__(self):
        self.calls = []
        self.on_send = None
        self.result = True

    def send_message(self, phone, text):
        self.calls.append((phone, text))
        if self.on_send:
            self.on_send()
        return self.result


class ProcessingRuntime(IngressRuntime):
    def __init__(self, factory, config):
        super().__init__(factory, config)
        self.agent = ScriptedAgent()
        self.outbound_broker = ScriptedOutboundBroker()
        self.transport = ScriptedTransport()
        self.sessions = []
        self.session_hooks = {}
        self.persistent_session_hooks = {}

    def session_factory(self):
        from contextlib import contextmanager
        @contextmanager
        def session_scope():
            with super(ProcessingRuntime, self).session_factory() as session:
                observed = BarrierSession(session)
                observed.hooks.update(self.persistent_session_hooks)
                observed.hooks.update(self.session_hooks)
                self.session_hooks.clear()
                self.sessions.append(observed)
                yield observed
        return session_scope()

    def buffer(self, content="Mensagem sintética", *, kind="text", phone="5551999990000", message_id=None):
        from uuid import uuid4
        from app.conversation_state import SenderIdentity
        with self.store.contact_lease(phone) as lease, self.session_factory() as db:
            self.coordinator.accept_ingress(db, SenderIdentity(phone, False, message_id or str(uuid4()), "pn"),
                kind, content, self.clock.now(), lease, self.processing_broker)
        return self.processing_broker.calls[-1]

    def seed_contact(self, phone, *, age_minutes=0, paused_hours=None, appointment=False):
        """Disposable fixtures retain a genuine central anchor and SQL rows."""
        from uuid import uuid4
        from app.models import ConversationContext, Appointment
        with self.store.contact_lease(phone) as lease, self._factory() as db:
            assert db.bind.url.database in (None, "", ":memory:")
            self.coordinator.resolve_ingress(db, phone, self.clock.now(), lease)
            db.add(ConversationContext(phone=phone, messages=[{"role": "user", "content": "synthetic history"}],
                last_activity=(self.clock.now() - timedelta(minutes=age_minutes)).replace(tzinfo=None),
                created_at=self.clock.now().replace(tzinfo=None)))
            if appointment:
                db.add(Appointment(patient_name="Synthetic", patient_phone=phone, patient_birth_date="01/01/2000",
                    appointment_date="20260914", appointment_time="12:00",
                    created_at=self.clock.now().replace(tzinfo=None), updated_at=self.clock.now().replace(tzinfo=None)))
            db.commit()
            if paused_hours is not None:
                self.coordinator.pause_manual(db, phone, paused_hours, "secretary_dashboard_pause",
                    self.clock.now(), lease, str(uuid4()))


def compose_recovery_fixture(runtime, monkeypatch):
    """Real composition with controlled probe outcomes, without parallel types."""
    from types import SimpleNamespace
    from app.conversation_recovery import compose_runtime
    composed = compose_runtime(settings=SimpleNamespace(webhook_secret="synthetic-webhook-secret"),
        client=runtime.store.client, config=runtime.store.config, session_factory=runtime.session_factory,
        sql_probe=lambda: True, agent=runtime.agent, processing_broker=runtime.processing_broker,
        outbound_broker=runtime.outbound_broker, transport=runtime.transport, clock=runtime.clock)
    probes = composed.readiness_status.__self__._probes
    for name, probe in tuple(probes.items()):
        monkeypatch.setitem(probes, name, lambda name=name, probe=probe: runtime.dependencies[name] and probe())
    return composed


class RecoveryWriteAudit:
    """Observe every executed Redis plan, not a mirrored transition implementation.

    Readiness must have been observed after the last plan-building dependency
    read. Only an explicitly witnessed broker ACK or persisted outbound receipt
    can exempt its identity-bound record. Lease maintenance is not recovery DML.
    """
    def __init__(self, runtime, monkeypatch):
        import sys
        from app.conversation_redis import ATOMIC_SCRIPT, RECOVERY_CHECKPOINT_SCRIPT
        self.observed, self.violations, self.opcodes = set(), [], set()
        self.checks = 0
        self.witnesses = []
        self.client = runtime.store.client
        readiness = runtime.readiness_status
        def probe():
            result = readiness()
            self.checks += int(result.ready)
            return result
        monkeypatch.setattr(runtime, "readiness_status", probe)
        client = runtime.store.client
        original_scan, original_eval, original_get = client.scan_iter, client.eval, client.get
        def get(*args, **kwargs):
            result = original_get(*args, **kwargs)
            self.checks = 0
            return result
        monkeypatch.setattr(client, "get", get)
        def scan(*args, **kwargs):
            result = original_scan(*args, **kwargs)
            frame = sys._getframe(1)
            while frame is not None and frame.f_code.co_name != "_atomic":
                frame = frame.f_back
            if frame is not None:
                self.checks = 0
            return result
        monkeypatch.setattr(client, "scan_iter", scan)
        def evaluate(script, count, *args):
            if script == RECOVERY_CHECKPOINT_SCRIPT:
                if not self.checks:
                    self.violations.append("checkpoint")
                self.observed.add("checkpoint")
                self.opcodes.add("SET")
            if script == ATOMIC_SCRIPT:
                keys, plan = args[:count], json.loads(args[count])
                operation = plan["operation"]
                self.observed.add(operation)
                failed = [check for check in plan["checks"] if
                    (original_get(keys[check["key"] - 1]) if check["op"] == "get" else
                     client.sismember(keys[check["key"] - 1], check["member"])) != check["value"]]
                blocked = bool(failed and failed[0].get("failure") != "generation")
                writes = plan["writes"] + plan.get("deadline_writes", [])
                repair = plan["quarantine"] or any(check.get("failure") == "generation" and
                    (original_get(keys[check["key"] - 1]) if check["op"] == "get" else
                     client.sismember(keys[check["key"] - 1], check["member"])) != check["value"]
                    for check in plan["checks"])
                if repair:
                    writes += plan.get("quarantine_writes", [])
                    self.opcodes.update(("SET", "SADD"))  # Corruption-quarantine anchor/index.
                self.opcodes.update(write["op"] for write in writes)
                lease_only = operation in ("acquire", "assert", "renew", "release")
                acknowledged_record = (not repair and not plan.get("deadline_writes")
                                       and self._identity_bound_plan(keys, plan))
                if (writes or repair) and not blocked and not lease_only and not acknowledged_record and not self.checks:
                    self.violations.append(operation)
                if acknowledged_record and (plan["quarantine"] or plan.get("quarantine_writes") or
                        any(check.get("failure") == "generation" for check in plan["checks"])):
                    self.violations.append("ack_can_destroy_evidence")
            return original_eval(script, count, *args)
        monkeypatch.setattr(client, "eval", evaluate)

    def witness_processing(self, command):
        """Called only by the fake broker when it actually recognizes the ACK."""
        from app.conversation_redis import contact_keys
        key = contact_keys(command.phone).batch_prefix + command.batch_id
        record = json.loads(self.client.values[key])
        assert record["body"]["enqueue_attempt_id"]
        self.witnesses.append(("processing", command.phone, record))

    def witness_outbound(self, command):
        """Independently observe a previously persisted exact outbound receipt."""
        from app.conversation_redis import contact_keys
        records = [json.loads(value) for key, value in self.client.values.items()
                   if key.startswith(contact_keys(command.phone).processing_prefix)]
        record = next(record for record in records if record["body"]["batch_id"] == command.batch_id)
        receipt = record["body"]["outbound_reservation"]
        assert record["body"]["outbound_attempted"] is True
        assert record["body"]["outbound_attempt_id"] == receipt["reservation_id"]
        self.witnesses.append(("outbound", command.phone, deepcopy(receipt)))

    def _identity_bound_plan(self, keys, plan):
        """Project the real opcodes and compare effects, without operation names.

        SET-over-DEL of unchanged manifest details is not evidence loss. Only
        one witnessed batch's ACK delta, or its proven outbound completion, is
        allowed; expiry/quarantine and unrelated receipt/index changes are not.
        """
        from app.conversation_redis import contact_keys, contact_digest, INDEX_KEYS
        values, indexes = dict(self.client.values), deepcopy(self.client.sets)
        for write in plan["writes"]:
            key, op = keys[write["key"] - 1], write["op"]
            if op == "SET":
                values[key] = write["value"]
            elif op == "DEL":
                values.pop(key, None)
                indexes.pop(key, None)
            elif op in ("SADD", "SREM"):
                members = indexes.setdefault(key, set())
                (members.add if op == "SADD" else members.discard)(write["value"])
            else:
                return False
        old_values = self.client.values
        changed = {key for key in old_values.keys() | values.keys() if old_values.get(key) != values.get(key)}
        checks = {keys[c["key"] - 1]: c["value"] for c in plan["checks"]
                  if c["op"] == "get" and c.get("failure") == "stale"}
        for kind, phone, witness in self.witnesses:
            contact = contact_keys(phone)
            batch_id = witness["entry"]["id"] if kind == "processing" else witness["batch_id"]
            batch_key = contact.batch_prefix + batch_id
            try:
                batch = json.loads(old_values[batch_key])
                updated_batch = json.loads(values[batch_key])
                old_anchor, new_anchor = json.loads(old_values[contact.anchor]), json.loads(values[contact.anchor])
                if (checks.get(batch_key) != old_values[batch_key]
                        or checks.get(contact.anchor) != old_values[contact.anchor]
                        or new_anchor["contact_revision"] != old_anchor["contact_revision"] + 1
                        or any(new_anchor.get(field) != old_anchor.get(field) for field in
                               ("cycle", "last_generation", "generation_history", "mutation_fence"))):
                    continue
                allowed = {batch_key, contact.anchor, contact.generation}
                expected_indexes = deepcopy(self.client.sets)
                if kind == "processing":
                    expected_body = {**witness["body"], "phase": "STAGED" if witness["body"]["phase"] == "STAGED" else "SCHEDULED",
                                     "scheduled_at": updated_batch["body"]["scheduled_at"]}
                    if (batch != witness or updated_batch["body"] != expected_body
                            or updated_batch["terminal"] or updated_batch["body"]["scheduled_at"] is None):
                        continue
                else:
                    processing_key = contact.processing_prefix + witness["processing_id"]
                    processing = json.loads(old_values[processing_key])
                    updated_processing = json.loads(values[processing_key])
                    body, after = processing["body"], updated_processing["body"]
                    if (checks.get(processing_key) != old_values[processing_key]
                            or body.get("outbound_reservation") != witness or body.get("outbound_attempted") is not True
                            or body.get("outbound_attempt_id") != witness["reservation_id"]
                            or body["claim_token"] != witness["claim_token"] or body["phase"] != "APPLYING"
                            or batch["body"]["phase"] != "STAGED"):
                        continue
                    allowed.add(processing_key)
                    completed = updated_batch["body"]["phase"] == "PROCESSED"
                    expected_body = {**body, "phase": "DONE"} if completed else {
                        **body, "owner_token_hash": after["owner_token_hash"]}
                    if (after != expected_body or updated_batch["body"] != {
                            **batch["body"], "phase": "PROCESSED" if completed else "STAGED"}
                            or updated_processing["terminal"] is not completed
                            or updated_batch["terminal"] is not completed):
                        continue
                    if completed:
                        allowed.update((contact.staging_prefix + batch_id, contact.buffer_prefix + batch_id))
                        for key, value in old_values.items():
                            if not key.startswith(contact.dedupe):
                                continue
                            receipt = json.loads(value)
                            if receipt["body"].get("batch_id") == batch_id:
                                terminal = json.loads(values[key])
                                if terminal["body"] != {**receipt["body"], "disposition": "PROCESSED"} or not terminal["terminal"]:
                                    break
                                allowed.add(key)
                        for flag in batch["entry"]["index_flags"]:
                            member = f"{contact_digest(phone)}:batch:{batch_id}"
                            expected_indexes.setdefault(INDEX_KEYS[flag], set()).discard(member)
                if changed <= allowed and {key: value for key, value in indexes.items() if value} == {
                        key: value for key, value in expected_indexes.items() if value}:
                    return True
            except (KeyError, TypeError, ValueError):
                continue
        return False


class RetryTask:
    """Celery Retry must escape the wrapper exactly once."""
    def __init__(self):
        from types import SimpleNamespace
        self.request = SimpleNamespace(id="synthetic-task", retries=0)
        self.calls = []

    def retry(self, **kwargs):
        from celery.exceptions import Retry
        self.calls.append(kwargs)
        raise Retry("synthetic retry")
