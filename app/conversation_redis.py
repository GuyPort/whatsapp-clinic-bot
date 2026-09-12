"""Fenced coordination primitives. Construction never connects or reads settings.

The injected synchronous Redis client implements ping/info/get/sismember/scan_iter/sscan_iter/
eval. This keyspace targets a single Redis primary (not Redis Cluster). Only the
atomic script writes Redis; it checks snapshots before applying a command batch.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from threading import Event, RLock, Thread
from typing import Callable
from uuid import UUID, uuid4

from app.conversation_state import (
    ContactAnchor, ContactDetail, ContactLease, ContactLeaseLost,
    ContactLockUnavailable, ConversationConfig, ConversationCycle,
    ConversationGenerationUnavailable, ConversationStateUnavailable,
    DependencyName, DependencyStatus, FailureReason, InvalidCanonicalContact,
    ManifestEntry, ReadinessReport, ReadinessUnavailable,
    MutationAttempt, MutationTarget, MutationPhase, ConversationMutationPending,
    ConversationMutationAborted,
    DefinitiveRollbackProof, _consume_rollback_proof,
)
from app.utils import normalize_phone

GLOBAL_EPOCH_KEY = "conversation:coordination:epoch"
DISPATCH_INDEX_KEY = "conversation:index:dispatch"
STAGING_INDEX_KEY = "conversation:index:staging"
QUARANTINE_INDEX_KEY = "conversation:index:quarantine"
INDEX_KEYS = {"dispatch": DISPATCH_INDEX_KEY, "staging": STAGING_INDEX_KEY}


@dataclass(frozen=True)
class ContactKeys:
    lease: str
    anchor: str
    generation: str
    dedupe: str
    batch_prefix: str
    buffer_prefix: str
    staging_prefix: str
    processing_prefix: str
    mutation_prefix: str


def contact_digest(phone: str) -> str:
    if not phone or normalize_phone(phone) != phone:
        raise InvalidCanonicalContact(FailureReason.INVALID_CANONICAL_CONTACT)
    return hashlib.sha256(phone.encode("utf-8")).hexdigest()


def contact_keys(phone: str) -> ContactKeys:
    prefix = f"conversation:contact:{contact_digest(phone)}"
    return ContactKeys(
        lease=f"{prefix}:lease", anchor=f"{prefix}:anchor",
        generation=f"{prefix}:generation", dedupe=f"{prefix}:dedupe",
        batch_prefix=f"{prefix}:batch:", buffer_prefix=f"{prefix}:buffer:",
        staging_prefix=f"{prefix}:staging:", processing_prefix=f"{prefix}:processing:",
        mutation_prefix=f"{prefix}:mutation:",
    )


# All checked keys and command targets are passed in KEYS. Values use ARGV only.
# ACL, argument and type preflight precede every write, including quarantine:
# Lua runtime errors do not roll back Redis writes. Requires redis.acl_check_cmd.
ATOMIC_SCRIPT = r'''
local p = cjson.decode(ARGV[1])
if type(redis.acl_check_cmd) ~= 'function' then return 'unavailable' end
local function info(section, field)
    return string.match(redis.call('INFO', section), field .. ':([^\r\n]+)')
end
if redis.call('GET', KEYS[p.epoch_key]) ~= p.epoch
   or info('server', 'run_id') ~= p.run_id
   or info('memory', 'maxmemory_policy') ~= 'noeviction'
   or info('persistence', 'aof_enabled') ~= '1'
   or info('persistence', 'aof_last_write_status') ~= 'ok'
   or info('persistence', 'loading') ~= '0' then return 'readiness' end
for _, c in ipairs(p.checks) do
    local kind = redis.call('TYPE', KEYS[c.key]).ok
    if kind ~= 'none' and kind ~= (c.op == 'get' and 'string' or 'set') then
        return 'unavailable'
    end
end
local function key_at(index)
    if type(index) ~= 'number' or index ~= math.floor(index) then return nil end
    return KEYS[index]
end
local function valid_ttl(ttl)
    return type(ttl) == 'number' and ttl >= 1 and ttl <= 9007199254740991
           and ttl == math.floor(ttl)
end
local function command(w)
    if type(w) ~= 'table' then return nil end
    local key = key_at(w.key)
    if not key then return nil end
    if w.op == 'DEL' then return {'DEL', key} end
    if w.op == 'PEXPIRE' then
        if not valid_ttl(w.ttl) then return nil end
        return {'PEXPIRE', key, string.format('%.0f', w.ttl)}
    end
    if type(w.value) ~= 'string' then return nil end
    if w.op == 'SADD' or w.op == 'SREM' then return {w.op, key, w.value} end
    if w.op ~= 'SET' and w.op ~= 'ACQUIRE' then return nil end
    if w.op == 'ACQUIRE' and (#p.writes ~= 1 or not valid_ttl(w.ttl)) then return nil end
    if w.ttl ~= nil then
        if not valid_ttl(w.ttl) then return nil end
        local args = {'SET', key, w.value, 'PX', string.format('%.0f', w.ttl)}
        if w.op == 'ACQUIRE' then table.insert(args, 'NX') end
        return args
    end
    return {'SET', key, w.value}
end
local function permitted(args)
    local ok, allowed = pcall(redis.acl_check_cmd, unpack(args))
    return ok and allowed == true
end
local anchor_key, quarantine_key = key_at(p.anchor_key), key_at(p.quarantine_key)
if not anchor_key or not quarantine_key or type(p.digest) ~= 'string' then return 'unavailable' end
local quarantine_commands = {
    {'SET', anchor_key, '{"cycle":"QUARANTINED"}'},
    {'SADD', quarantine_key, p.digest}
}
if not permitted(quarantine_commands[1]) or not permitted(quarantine_commands[2]) then
    return 'unavailable'
end
local quarantine_type = redis.call('TYPE', quarantine_key).ok
if quarantine_type ~= 'none' and quarantine_type ~= 'set' then return 'unavailable' end
local commands, kinds = {}, {}
for _, w in ipairs(p.writes) do
    local args = command(w)
    if not args or not permitted(args) then return 'unavailable' end
    local key, op = args[2], args[1]
    local kind = kinds[key] or redis.call('TYPE', key).ok
    if (op == 'SADD' or op == 'SREM') and kind ~= 'none' and kind ~= 'set' then
        return 'unavailable'
    end
    if op == 'SET' then kinds[key] = 'string'
    elseif op == 'DEL' then kinds[key] = 'none'
    elseif op == 'SADD' then kinds[key] = 'set' end
    table.insert(commands, args)
end
local function quarantine()
    for _, args in ipairs(quarantine_commands) do redis.call(unpack(args)) end
    return 'generation'
end
for _, c in ipairs(p.checks) do
    local actual
    if c.op == 'get' then actual = redis.call('GET', KEYS[c.key])
    else actual = redis.call('SISMEMBER', KEYS[c.key], c.member) end
    local expected = c.value
    if expected == cjson.null then expected = false end
    if actual ~= expected then
        if c.failure == 'generation' then return quarantine() end
        return c.failure
    end
end
if p.quarantine then return quarantine() end
if p.deadline_us ~= nil then
    if type(p.deadline_us) ~= 'number' or not permitted({'TIME'}) then return 'unavailable' end
    local current = redis.call('TIME')
    if tonumber(current[1]) * 1000000 + tonumber(current[2]) >= p.deadline_us then
        return 'pending'
    end
end
for i, args in ipairs(commands) do
    local result = redis.call(unpack(args))
    if p.writes[i].op == 'ACQUIRE' and not result then return 'locked' end
end
return 'ok'
'''


def _text(value):
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _entry_data(entry: ManifestEntry) -> dict:
    if (entry.kind not in ("batch", "buffer", "staging", "processing", "mutation", "dedupe")
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", entry.id)
            or type(entry.version) is not int or entry.version < 1
            or entry.expected_until.tzinfo is None
            or any(flag not in INDEX_KEYS for flag in entry.index_flags)
            or len(set(entry.index_flags)) != len(entry.index_flags)):
        raise ValueError("invalid_value")
    return {"kind": entry.kind, "id": entry.id, "version": entry.version,
            "expected_until": int(entry.expected_until.timestamp() * 1000),
            "index_flags": sorted(entry.index_flags)}


def _entry_load(value: dict) -> ManifestEntry:
    entry = ManifestEntry(value["kind"], value["id"], value["version"],
                          datetime.fromtimestamp(value["expected_until"] / 1000, timezone.utc),
                          tuple(value["index_flags"]))
    _entry_data(entry)
    return entry


def manifest_fingerprint(entries: tuple[ManifestEntry, ...]) -> str:
    rows = []
    for entry in entries:
        data = _entry_data(entry)
        rows.append(f"{data['kind']}:{data['id']}:{data['version']}:"
                    f"{data['expected_until']}:{','.join(data['index_flags'])}")
    return hashlib.sha256("\n".join(sorted(rows)).encode()).hexdigest()


def _anchor_data(anchor: ContactAnchor) -> dict:
    data = {"contact_revision": anchor.contact_revision,
            "last_generation": str(anchor.last_generation),
            "manifest": [_entry_data(entry) for entry in anchor.manifest],
            "manifest_fingerprint": anchor.manifest_fingerprint,
            "generation_history": [str(value) for value in anchor.generation_history],
            "cycle": anchor.cycle.value}
    if anchor.mutation_fence is not None:
        data["mutation_fence"] = dict(anchor.mutation_fence)
    return data


def _generation_control(anchor: ContactAnchor) -> str:
    """A durable second fence, cross-checked even when the manifest is empty."""
    data = {"last_generation": str(anchor.last_generation),
                  "contact_revision": anchor.contact_revision,
                  "manifest_fingerprint": anchor.manifest_fingerprint}
    if anchor.mutation_fence is not None:
        data["mutation_fingerprint"] = hashlib.sha256(_json(dict(anchor.mutation_fence)).encode()).hexdigest()
    return _json(data)


class LeaseHeartbeat:
    """One daemon per top-level lease; failure is sticky until context exit."""

    def __init__(self, renew: Callable[[], None], interval: float,
                 wait: Callable[[Event, float], bool] | None = None):
        self.renew, self.interval = renew, interval
        self._wait = wait or (lambda event, timeout: event.wait(timeout))
        self._stop, self._lost = Event(), Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._wait(self._stop, self.interval):
            try:
                self.renew()
            except Exception:
                self._lost.set()
                return

    def assert_owned(self) -> None:
        if self._lost.is_set() or self._stop.is_set():
            raise ContactLeaseLost(FailureReason.CONTACT_LEASE_LOST)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval)
            if self._thread.is_alive():
                self._lost.set()


class RedisConversationStore:
    def __init__(self, client, config: ConversationConfig, clock=None,
                 heartbeat_wait=None):
        self.client, self.config = client, config
        self.clock = clock
        self.heartbeat_wait = heartbeat_wait
        self._heartbeats: dict[str, LeaseHeartbeat] = {}
        self._lock = RLock()

    def _now(self) -> datetime:
        return self.clock.now() if self.clock else datetime.now(timezone.utc)

    def readiness(self) -> ReadinessReport:
        ready = False
        try:
            server = self.client.info("server")
            memory = self.client.info("memory")
            persistence = self.client.info("persistence")
            ready = bool(
                self.client.ping() and not self.config.issues
                and _text(server.get("run_id")) == self.config.redis_expected_run_id
                and _text(memory.get("maxmemory_policy")) == "noeviction"
                and self.config.redis_attest_noeviction
                and self.config.redis_attest_persistence
                and persistence.get("aof_enabled") == 1
                and _text(persistence.get("aof_last_write_status")) == "ok"
                and persistence.get("loading") == 0
                and _text(self.client.get(GLOBAL_EPOCH_KEY)) == str(self.config.coordination_epoch)
            )
        except Exception:
            pass
        return ReadinessReport((DependencyStatus(DependencyName.REDIS, ready),
                                DependencyStatus(DependencyName.EPOCH, ready)))

    def _ready(self) -> None:
        if not self.readiness().ready:
            raise ReadinessUnavailable(FailureReason.READINESS_UNAVAILABLE)

    def _atomic(self, phone, operation, checks=(), writes=(), quarantine=False, *, deadline=None):
        self._ready()
        keys = contact_keys(phone)
        key_list = [GLOBAL_EPOCH_KEY, keys.anchor, QUARANTINE_INDEX_KEY]
        def index(key):
            if key not in key_list:
                key_list.append(key)
            return key_list.index(key) + 1
        plan = {"operation": operation, "epoch_key": 1, "anchor_key": 2,
                "quarantine_key": 3, "epoch": str(self.config.coordination_epoch),
                "run_id": self.config.redis_expected_run_id, "digest": contact_digest(phone),
                "quarantine": quarantine,
                "checks": [{**c, "key": index(c["key"])} for c in checks],
                "writes": [{**w, "key": index(w["key"])} for w in writes]}
        if deadline is not None:
            plan["deadline_us"] = int(deadline.timestamp() * 1000000)
        try:
            result = _text(self.client.eval(ATOMIC_SCRIPT, len(key_list), *key_list, _json(plan)))
        except Exception:
            raise ConversationStateUnavailable(FailureReason.STATE_UNAVAILABLE) from None
        errors = {
            "readiness": (ReadinessUnavailable, FailureReason.READINESS_UNAVAILABLE),
            "lease": (ContactLeaseLost, FailureReason.CONTACT_LEASE_LOST),
            "locked": (ContactLockUnavailable, FailureReason.CONTACT_LOCK_UNAVAILABLE),
            "generation": (ConversationGenerationUnavailable, FailureReason.GENERATION_UNAVAILABLE),
            "pending": (ConversationMutationPending, FailureReason.MUTATION_PENDING),
        }
        if result != "ok":
            error, reason = errors.get(result, (ConversationStateUnavailable, FailureReason.STATE_UNAVAILABLE))
            raise error(reason)

    @staticmethod
    def _check(key, value, failure="stale"):
        return {"op": "get", "key": key, "value": value, "failure": failure}

    def _lease_check(self, lease):
        return self._check(contact_keys(lease.phone).lease, lease.owner_token, "lease")

    @contextmanager
    def contact_lease(self, phone: str):
        self._ready()
        keys = contact_keys(phone)
        token = uuid4().hex
        ttl = self.config.contact_lease_ttl_seconds
        self._atomic(phone, "acquire", writes=[
            {"op": "ACQUIRE", "key": keys.lease, "value": token, "ttl": ttl * 1000}])
        lease = ContactLease(phone, token, self._now() + timedelta(seconds=ttl),
                             lambda: self.assert_owned(lease))
        heartbeat = LeaseHeartbeat(lambda: self.renew_lease(lease), ttl / 3, self.heartbeat_wait)
        with self._lock:
            self._heartbeats[token] = heartbeat
        heartbeat.start()
        try:
            yield lease
        finally:
            heartbeat.stop()
            with self._lock:
                self._heartbeats.pop(token, None)
            self.release_lease(lease)

    def assert_owned(self, lease: ContactLease) -> None:
        with self._lock:
            heartbeat = self._heartbeats.get(lease.owner_token)
        if heartbeat is None:
            raise ContactLeaseLost(FailureReason.CONTACT_LEASE_LOST)
        heartbeat.assert_owned()
        self._atomic(lease.phone, "assert", [self._lease_check(lease)])

    def release_lease(self, lease: ContactLease) -> None:
        try:
            self._atomic(lease.phone, "release", [self._lease_check(lease)],
                         [{"op": "DEL", "key": contact_keys(lease.phone).lease}])
        except (ContactLeaseLost, ReadinessUnavailable, ConversationStateUnavailable):
            # Release cannot hide the protected operation's error; TTL remains bounded.
            pass

    def _detail_key(self, phone, entry):
        keys = contact_keys(phone)
        _entry_data(entry)
        prefix = keys.dedupe + ":" if entry.kind == "dedupe" else getattr(keys, entry.kind + "_prefix")
        return prefix + entry.id

    def _member(self, phone, entry):
        return f"{contact_digest(phone)}:{entry.kind}:{entry.id}"

    def _get(self, key):
        try:
            return _text(self.client.get(key))
        except Exception:
            raise ConversationStateUnavailable(FailureReason.STATE_UNAVAILABLE) from None

    def _compact_mutation_fence(self, lease, anchor, checks):
        """Recover only compact metadata, even after bounded detail payloads vanished."""
        body = {**anchor.mutation_fence, "phase": MutationPhase.QUARANTINED.value,
                "paused_until": None, "reason": None}
        body.pop("detail_fingerprint", None)
        prior = next(item for item in anchor.manifest if item.kind == "mutation"
                     and item.id == self._attempt_id(body["operation_id"]))
        entry = replace(prior, version=prior.version + 1, index_flags=())
        receipts = []
        for candidate in anchor.manifest:
            if candidate == prior or candidate.kind != "mutation" or self._now() >= candidate.expected_until:
                continue
            key = self._detail_key(lease.phone, candidate)
            raw = self._get(key)
            checks.append(self._check(key, raw, "generation"))
            try:
                value = json.loads(raw)
                detail = ContactDetail(candidate, value["body"], value["terminal"])
                if value["entry"] != _entry_data(candidate) or not self._is_replay_receipt(detail):
                    raise ValueError("invalid_value")
                receipts.append(detail)
            except (KeyError, TypeError, ValueError, ConversationStateUnavailable):
                self._atomic(lease.phone, "validate", checks, quarantine=True)
        entries = tuple(sorted((entry, *(item.entry for item in receipts)), key=lambda item: (item.kind, item.id)))
        updated = replace(anchor, contact_revision=anchor.contact_revision + 1,
                          manifest=entries, manifest_fingerprint=manifest_fingerprint(entries),
                          cycle=ConversationCycle.QUARANTINED,
                          mutation_fence={**body, "detail_fingerprint": hashlib.sha256(_json(body).encode()).hexdigest()})
        writes = []
        for item in anchor.manifest:
            writes.append({"op": "DEL", "key": self._detail_key(lease.phone, item)})
            for flag in item.index_flags:
                writes.append({"op": "SREM", "key": INDEX_KEYS[flag], "value": self._member(lease.phone, item)})
        keys = contact_keys(lease.phone)
        for receipt in receipts:
            writes.append({"op": "SET", "key": self._detail_key(lease.phone, receipt.entry),
                           "value": _json({"entry": _entry_data(receipt.entry), "body": dict(receipt.body), "terminal": True}),
                           "ttl": int((receipt.entry.expected_until + timedelta(seconds=self.config.ttl_margin_seconds)
                                       - self._now()).total_seconds() * 1000)})
        writes.extend([
            {"op": "SET", "key": self._detail_key(lease.phone, entry),
             "value": _json({"entry": _entry_data(entry), "body": body, "terminal": False})},
            {"op": "SET", "key": keys.anchor, "value": _json(_anchor_data(updated))},
            {"op": "SET", "key": keys.generation, "value": _generation_control(updated)},
            {"op": "SADD", "key": QUARANTINE_INDEX_KEY, "value": contact_digest(lease.phone)},
        ])
        self._atomic(lease.phone, "quarantine_mutation", checks, writes)
        raise ConversationMutationPending(FailureReason.MUTATION_PENDING)

    def _trim_quarantine_receipts(self, lease, anchor, checks):
        current = self._attempt_id(anchor.mutation_fence["operation_id"])
        expired = tuple(item for item in anchor.manifest if item.kind == "mutation"
                        and item.id != current and self._now() >= item.expected_until)
        if not expired:
            return False
        retained = tuple(item for item in anchor.manifest if item not in expired)
        updated = replace(anchor, contact_revision=anchor.contact_revision + 1,
                          manifest=retained, manifest_fingerprint=manifest_fingerprint(retained))
        keys = contact_keys(lease.phone)
        writes = [{"op": "DEL", "key": self._detail_key(lease.phone, item)} for item in expired]
        writes.extend([{"op": "SET", "key": keys.anchor, "value": _json(_anchor_data(updated))},
                       {"op": "SET", "key": keys.generation, "value": _generation_control(updated)}])
        self._atomic(lease.phone, "trim_quarantine_receipts", checks, writes)
        return True

    def _snapshot(self, lease, *, operational=False):
        self.assert_owned(lease)
        keys = contact_keys(lease.phone)
        raw = self._get(keys.anchor)
        checks = [self._lease_check(lease), self._check(keys.anchor, raw)]
        if raw is None:
            if self._has_remnants(lease.phone):
                self._atomic(lease.phone, "validate", checks, quarantine=True)
            raise ConversationGenerationUnavailable(FailureReason.GENERATION_UNAVAILABLE)
        try:
            value = json.loads(raw)
            if (not isinstance(value, dict)
                    or not isinstance(value["last_generation"], str)
                    or not isinstance(value["generation_history"], list)
                    or not all(isinstance(g, str) for g in value["generation_history"])):
                raise ValueError("invalid_value")
            entries = tuple(_entry_load(e) for e in value["manifest"])
            anchor = ContactAnchor(value["contact_revision"], UUID(value["last_generation"]),
                                   entries, value["manifest_fingerprint"],
                                   tuple(UUID(g) for g in value["generation_history"]),
                                   ConversationCycle(value["cycle"]), value.get("mutation_fence"))
            if (type(anchor.contact_revision) is not int or anchor.contact_revision < 0
                    or anchor.last_generation not in anchor.generation_history
                    or len(set(anchor.generation_history)) != len(anchor.generation_history)
                    or len({(e.kind, e.id) for e in entries}) != len(entries)
                    or manifest_fingerprint(entries) != anchor.manifest_fingerprint):
                raise ValueError("invalid_value")
        except (KeyError, TypeError, ValueError, OverflowError, ConversationGenerationUnavailable):
            self._atomic(lease.phone, "validate", checks, quarantine=True)
            raise AssertionError("unreachable")
        checks.append(self._check(keys.generation, _generation_control(anchor), "generation"))
        fence = anchor.mutation_fence
        if fence is not None:
            try:
                identity = self._attempt_id(fence["operation_id"])
                entry = next(item for item in entries if item.kind == "mutation" and item.id == identity)
                attempt = self._attempt_load(ContactDetail(entry, fence))
                if attempt.phase not in (MutationPhase.PREPARED, MutationPhase.COMMITTING, MutationPhase.QUARANTINED):
                    raise ValueError("invalid_value")
            except (KeyError, TypeError, StopIteration, ValueError, ConversationStateUnavailable):
                self._atomic(lease.phone, "validate", checks, quarantine=True)
                raise AssertionError("unreachable")
            if attempt.phase is MutationPhase.COMMITTING and self._now() >= attempt.processing_deadline:
                self._compact_mutation_fence(lease, anchor, checks)
        if anchor.cycle is ConversationCycle.QUARANTINED:
            if not operational:
                self._atomic(lease.phone, "validate", checks)
                raise ConversationMutationPending(FailureReason.MUTATION_PENDING)
            if fence is not None and self._trim_quarantine_receipts(lease, anchor, checks):
                return self._snapshot(lease, operational=True)
        details = []
        for entry in entries:
            key = self._detail_key(lease.phone, entry)
            raw_detail = self._get(key)
            checks.append(self._check(key, raw_detail, "generation"))
            try:
                data = json.loads(raw_detail)
                if data["entry"] != _entry_data(entry) or type(data["terminal"]) is not bool:
                    raise ValueError("invalid_value")
                if not isinstance(data["body"], dict):
                    raise ValueError("invalid_value")
                detail = ContactDetail(entry, data["body"], data["terminal"])
                if fence is not None and entry.kind == "mutation" and entry.id == self._attempt_id(fence["operation_id"]):
                    if (data["terminal"] is not False
                            or self._mutation_receipt(self._attempt_load(detail)) != fence
                            or hashlib.sha256(_json(data["body"]).encode()).hexdigest() != fence.get("detail_fingerprint")):
                        raise ValueError("invalid_value")
                details.append(detail)
            except (TypeError, KeyError, ValueError, ConversationStateUnavailable):
                if fence is not None and fence["phase"] == MutationPhase.COMMITTING.value:
                    self._compact_mutation_fence(lease, anchor, checks)
                self._atomic(lease.phone, "validate", checks, quarantine=True)
                raise AssertionError("unreachable")
            for flag in entry.index_flags:
                checks.append({"op": "member", "key": INDEX_KEYS[flag],
                               "member": self._member(lease.phone, entry), "value": 1,
                               "failure": "generation"})
        self._atomic(lease.phone, "validate", checks)
        if anchor.cycle is ConversationCycle.QUARANTINED and not operational:
            raise ConversationMutationPending(FailureReason.MUTATION_PENDING)
        return anchor, tuple(details), checks

    def initialize_contact(self, phone: str, lease: ContactLease | None = None,
                           *, db_state_present: bool | None = None) -> ContactAnchor:
        self._ready()
        if lease is None or lease.phone != phone:
            raise ConversationGenerationUnavailable(FailureReason.GENERATION_UNAVAILABLE)
        self.assert_owned(lease)
        keys = contact_keys(phone)
        if self._get(keys.anchor) is not None:
            return self.read_anchor(lease)
        if db_state_present is not False:
            raise ConversationGenerationUnavailable(FailureReason.GENERATION_UNAVAILABLE)
        # Any remnant indicates loss. The later coordinator must check BOTH SQL tables.
        if self._has_remnants(phone):
            self._atomic(phone, "initialize", [self._lease_check(lease), self._check(keys.anchor, None)],
                         quarantine=True)
        generation = uuid4()
        anchor = ContactAnchor(0, generation, (), manifest_fingerprint(()), (generation,))
        self._atomic(phone, "initialize", [self._lease_check(lease),
                     self._check(keys.anchor, None), self._check(keys.generation, None, "generation")],
                     [{"op": "SET", "key": keys.anchor, "value": _json(_anchor_data(anchor))},
                      {"op": "SET", "key": keys.generation, "value": _generation_control(anchor)}])
        return anchor

    def _has_remnants(self, phone: str) -> bool:
        keys = contact_keys(phone)
        prefix = keys.anchor.rsplit(":", 1)[0] + ":"
        digest = contact_digest(phone)
        try:
            if any(_text(key) != keys.lease for key in self.client.scan_iter(match=prefix + "*")):
                return True
            if self.client.sismember(QUARANTINE_INDEX_KEY, digest):
                return True
            return any(any(True for _ in self.client.sscan_iter(index, match=digest + ":*"))
                       for index in INDEX_KEYS.values())
        except Exception:
            raise ConversationStateUnavailable(FailureReason.STATE_UNAVAILABLE) from None

    def read_anchor(self, lease: ContactLease) -> ContactAnchor:
        return self._snapshot(lease)[0]

    def read_details(self, lease: ContactLease) -> tuple[ContactDetail, ...]:
        return self._snapshot(lease)[1]

    def _transition(self, lease, expected, details, generation=None, operation="cas", extra=(),
                    *, cycle=None, operational=False, deadline=None, mutation_fence=...):
        with self._lock:
            anchor, previous, checks = self._snapshot(lease, operational=operational)
            if (anchor.contact_revision != expected.contact_revision
                    or anchor.manifest_fingerprint != expected.manifest_fingerprint):
                raise ConversationStateUnavailable(FailureReason.STATE_UNAVAILABLE)
            generation = generation or anchor.last_generation
            if not isinstance(generation, UUID):
                raise ConversationStateUnavailable(FailureReason.INVALID_VALUE)
            if generation != anchor.last_generation and generation in anchor.generation_history:
                self._atomic(lease.phone, operation, checks, quarantine=True)
            previous_by_id = {(item.entry.kind, item.entry.id): item for item in previous}
            for item in details:
                old = previous_by_id.get((item.entry.kind, item.entry.id))
                if old is not None and (item.entry.version < old.entry.version or (
                        item != old and item.entry.version == old.entry.version)):
                    self._atomic(lease.phone, operation, checks, quarantine=True)
            entries = tuple(sorted((item.entry for item in details), key=lambda e: (e.kind, e.id)))
            if len({(e.kind, e.id) for e in entries}) != len(entries):
                raise ConversationStateUnavailable(FailureReason.INVALID_VALUE)
            fingerprint = manifest_fingerprint(entries)
            history = anchor.generation_history
            if generation != anchor.last_generation:
                history += (generation,)
            updated = replace(anchor, contact_revision=anchor.contact_revision + 1,
                              last_generation=generation, manifest=entries,
                              manifest_fingerprint=fingerprint, generation_history=history,
                              cycle=cycle or anchor.cycle)
            if mutation_fence is not ...:
                updated = replace(updated, mutation_fence=mutation_fence)
            writes = []
            for item in previous:
                writes.append({"op": "DEL", "key": self._detail_key(lease.phone, item.entry)})
                for flag in item.entry.index_flags:
                    writes.append({"op": "SREM", "key": INDEX_KEYS[flag],
                                   "value": self._member(lease.phone, item.entry)})
            for item in details:
                durable_quarantine = (updated.cycle is ConversationCycle.QUARANTINED
                                      and item.entry.kind == "mutation"
                                      and item.body.get("phase") == MutationPhase.QUARANTINED.value)
                horizon = item.entry.expected_until + timedelta(seconds=self.config.ttl_margin_seconds)
                ttl = int((horizon - self._now()).total_seconds() * 1000)
                if ttl <= 0 and not durable_quarantine:
                    raise ConversationStateUnavailable(FailureReason.INVALID_VALUE)
                write = {"op": "SET", "key": self._detail_key(lease.phone, item.entry),
                         "value": _json({"entry": _entry_data(item.entry),
                                         "body": dict(item.body), "terminal": item.terminal})}
                if not durable_quarantine:
                    write["ttl"] = ttl
                writes.append(write)
                for flag in item.entry.index_flags:
                    writes.append({"op": "SADD", "key": INDEX_KEYS[flag],
                                   "value": self._member(lease.phone, item.entry)})
            keys = contact_keys(lease.phone)
            if updated.cycle is ConversationCycle.QUARANTINED:
                writes.append({"op": "SADD", "key": QUARANTINE_INDEX_KEY,
                               "value": contact_digest(lease.phone)})
            elif operational:
                writes.append({"op": "SREM", "key": QUARANTINE_INDEX_KEY,
                               "value": contact_digest(lease.phone)})
            writes.extend([{"op": "SET", "key": keys.anchor, "value": _json(_anchor_data(updated))},
                           {"op": "SET", "key": keys.generation, "value": _generation_control(updated)}, *extra])
            self._atomic(lease.phone, operation, checks, writes, deadline=deadline)
            return updated

    def compare_and_set(self, lease: ContactLease, expected: ContactAnchor,
                        details: tuple[ContactDetail, ...], *, generation: UUID | None = None,
                        cycle: ConversationCycle | None = None) -> ContactAnchor:
        """Replace the complete detail set, fenced by the caller's anchor snapshot."""
        return self._transition(lease, expected, details, generation, cycle=cycle)

    def cleanup(self, lease: ContactLease, expected: ContactAnchor) -> ContactAnchor:
        anchor, details, _ = self._snapshot(lease)
        if (anchor.contact_revision, anchor.manifest_fingerprint) != (
                expected.contact_revision, expected.manifest_fingerprint):
            raise ConversationStateUnavailable(FailureReason.STATE_UNAVAILABLE)
        retained = tuple(item for item in details if not (
            item.terminal and self._now() >= item.entry.expected_until))
        return self._transition(lease, expected, retained) if len(retained) != len(details) else anchor

    @staticmethod
    def _attempt_id(operation_id: str) -> str:
        if not isinstance(operation_id, str) or not operation_id:
            raise ConversationStateUnavailable(FailureReason.INVALID_VALUE)
        return hashlib.sha256(operation_id.encode()).hexdigest()

    @staticmethod
    def _attempt_data(attempt: MutationAttempt) -> dict:
        return {"epoch": str(attempt.epoch), "operation_id": attempt.operation_id,
                "kind": attempt.kind, "phase": attempt.phase.value,
                "target_fingerprint": attempt.target_fingerprint,
                "request_fingerprint": attempt.request_fingerprint,
                "generation": str(attempt.generation), "prior_cycle": attempt.prior_cycle.value,
                "target_cycle": attempt.target_cycle.value,
                "processing_deadline": attempt.processing_deadline.isoformat(),
                "expected_until": attempt.expected_until.isoformat(),
                "owner_token_hash": attempt.owner_token_hash,
                "paused_until": attempt.paused_until.isoformat() if attempt.paused_until else None,
                "reason": attempt.reason,
                "started_at": attempt.started_at.isoformat() if attempt.started_at else None}

    def _attempt_load(self, item: ContactDetail) -> MutationAttempt:
        try:
            value = item.body
            attempt = MutationAttempt(
                UUID(value["epoch"]), value["operation_id"], value["kind"], MutationPhase(value["phase"]),
                value["target_fingerprint"], value["request_fingerprint"], UUID(value["generation"]),
                ConversationCycle(value["prior_cycle"]), ConversationCycle(value["target_cycle"]),
                datetime.fromisoformat(value["processing_deadline"]), datetime.fromisoformat(value["expected_until"]),
                value["owner_token_hash"], datetime.fromisoformat(value["paused_until"]) if value["paused_until"] else None,
                value["reason"], datetime.fromisoformat(value["started_at"]) if value.get("started_at") else None)
            if (attempt.epoch != self.config.coordination_epoch
                    or self._attempt_id(attempt.operation_id) != item.entry.id
                    or attempt.processing_deadline.tzinfo is None or attempt.expected_until.tzinfo is None):
                raise ValueError("invalid_value")
            return attempt
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ConversationStateUnavailable(FailureReason.STATE_UNAVAILABLE) from None

    def _mutation_snapshot(self, phone, operation_id, lease, *, operational=False):
        if lease.phone != phone:
            raise ContactLeaseLost(FailureReason.CONTACT_LEASE_LOST)
        anchor, details, _ = self._snapshot(lease, operational=operational)
        identity = self._attempt_id(operation_id)
        item = next((item for item in details if item.entry.kind == "mutation" and item.entry.id == identity), None)
        return anchor, details, item, self._attempt_load(item) if item else None

    def inspect_mutation(self, phone: str, operation_id: str, lease: ContactLease,
                         *, operational: bool = False) -> MutationAttempt | None:
        return self._mutation_snapshot(phone, operation_id, lease, operational=operational)[3]

    def _write_attempt(self, lease, anchor, details, previous, attempt, operation,
                       *, cycle=None, compact=False, operational=False, deadline=None):
        entry = ManifestEntry("mutation", self._attempt_id(attempt.operation_id),
                              previous.entry.version + 1 if previous else 1, attempt.expected_until)
        updated = ContactDetail(entry, self._attempt_data(attempt),
                                attempt.phase in (MutationPhase.COMMITTED, MutationPhase.ABORTED))
        retained = tuple(item for item in details if item != previous
                         and (not compact or self._is_replay_receipt(item)))
        fence = self._mutation_receipt(attempt) if attempt.phase in (
            MutationPhase.PREPARED, MutationPhase.COMMITTING, MutationPhase.QUARANTINED) else None
        self._transition(lease, anchor, (*retained, updated), attempt.generation,
                         operation=operation, cycle=cycle, operational=operational,
                         deadline=deadline, mutation_fence=fence)
        return attempt

    def _mutation_receipt(self, attempt: MutationAttempt) -> dict:
        return {**self._attempt_data(replace(attempt, paused_until=None, reason=None)),
                "detail_fingerprint": hashlib.sha256(_json(self._attempt_data(attempt)).encode()).hexdigest()}

    def _is_replay_receipt(self, item: ContactDetail) -> bool:
        return (item.entry.kind == "mutation" and item.terminal is True
                and self._now() < item.entry.expected_until
                and self._attempt_load(item).phase in (MutationPhase.COMMITTED, MutationPhase.ABORTED))

    def assert_mutation_available(self, lease: ContactLease, now: datetime,
                                  *, operation_id: str | None = None) -> None:
        _, details, _ = self._snapshot(lease)
        for item in details:
            if item.entry.kind != "mutation":
                continue
            attempt = self._attempt_load(item)
            if attempt.phase is MutationPhase.PREPARED and attempt.operation_id == operation_id:
                continue
            if attempt.phase is MutationPhase.COMMITTING and self._now() >= attempt.processing_deadline:
                self.quarantine_ambiguous_commit(lease.phone, attempt.operation_id, lease, now)
            if attempt.phase in (MutationPhase.PREPARED, MutationPhase.COMMITTING, MutationPhase.QUARANTINED):
                raise ConversationMutationPending(FailureReason.MUTATION_PENDING)

    def prepare_mutation(self, phone: str, kind: str, target_fingerprint: str,
                         lease: ContactLease, operation_id: str, now: datetime,
                         *, target: MutationTarget) -> MutationAttempt:
        with self._lock:
            anchor, details, previous, attempt = self._mutation_snapshot(phone, operation_id, lease)
            self.assert_mutation_available(lease, now, operation_id=operation_id)
            if target.kind != kind or target.fingerprint != target_fingerprint:
                raise ConversationStateUnavailable(FailureReason.INVALID_VALUE)
            if attempt is not None:
                if (attempt.request_fingerprint != target.request_fingerprint
                        or attempt.target_fingerprint != target_fingerprint
                        or attempt.target_cycle is not target.cycle):
                    raise ConversationStateUnavailable(FailureReason.INVALID_VALUE)
                if attempt.phase is MutationPhase.COMMITTED:
                    return attempt
                if attempt.phase is MutationPhase.ABORTED:
                    raise ConversationMutationAborted(FailureReason.MUTATION_ABORTED)
                if attempt.phase is MutationPhase.PREPARED:
                    if anchor.last_generation != attempt.generation:
                        raise ConversationGenerationUnavailable(FailureReason.GENERATION_UNAVAILABLE)
                    if self._now() >= attempt.processing_deadline:
                        self._write_attempt(lease, anchor, details, previous, replace(attempt, phase=MutationPhase.ABORTED),
                                            "abort_prepared", cycle=attempt.prior_cycle)
                        raise ConversationMutationAborted(FailureReason.MUTATION_ABORTED)
                    resumed = replace(attempt, owner_token_hash=hashlib.sha256(lease.owner_token.encode()).hexdigest())
                    return self._write_attempt(lease, anchor, details, previous, resumed,
                                               "resume_prepared", cycle=ConversationCycle.MUTATING,
                                               deadline=attempt.processing_deadline)
                raise ConversationMutationPending(FailureReason.MUTATION_PENDING)
            now = self._now()
            attempt = MutationAttempt(
                self.config.coordination_epoch, operation_id, kind, MutationPhase.PREPARED,
                target_fingerprint, target.request_fingerprint,
                uuid4() if target.rotate_generation else anchor.last_generation,
                anchor.cycle, target.cycle, now + timedelta(seconds=self.config.processing_retry_seconds),
                now + timedelta(seconds=self.config.replay_window_seconds),
                hashlib.sha256(lease.owner_token.encode()).hexdigest(), target.paused_until, target.reason, now)
            return self._write_attempt(lease, anchor, details, previous, attempt,
                                       "prepare_mutation", cycle=ConversationCycle.MUTATING)

    def enter_committing(self, phone: str, operation_id: str, lease: ContactLease,
                         now: datetime, processing_deadline: datetime) -> MutationAttempt:
        with self._lock:
            anchor, details, item, attempt = self._mutation_snapshot(phone, operation_id, lease)
            if (attempt is None or attempt.phase is not MutationPhase.PREPARED
                    or attempt.owner_token_hash != hashlib.sha256(lease.owner_token.encode()).hexdigest()
                    or processing_deadline != attempt.processing_deadline
                    or self._now() >= processing_deadline):
                raise ConversationMutationPending(FailureReason.MUTATION_PENDING)
            return self._write_attempt(lease, anchor, details, item,
                                       replace(attempt, phase=MutationPhase.COMMITTING),
                                       "enter_committing", cycle=ConversationCycle.MUTATING,
                                       deadline=processing_deadline)

    def preserve_or_abort_prepared(self, phone: str, operation_id: str,
                                  lease: ContactLease, now: datetime) -> None:
        """Called only after local rollback returned and commit never began."""
        with self._lock:
            anchor, details, item, attempt = self._mutation_snapshot(phone, operation_id, lease)
            if attempt is None or attempt.phase is not MutationPhase.PREPARED:
                return
            if attempt.owner_token_hash != hashlib.sha256(lease.owner_token.encode()).hexdigest():
                return
            self._write_attempt(lease, anchor, details, item, replace(attempt, phase=MutationPhase.ABORTED),
                                "abort_prepared", cycle=attempt.prior_cycle)

    def restore_prepared_after_rollback(self, phone: str, operation_id: str, lease: ContactLease,
                                        now: datetime, *, proof: DefinitiveRollbackProof) -> None:
        """A consumed same-process receipt proves SQL commit was never invoked."""
        if not _consume_rollback_proof(proof, operation_id, lease):
            raise ConversationStateUnavailable(FailureReason.INVALID_VALUE)
        with self._lock:
            anchor, details, item, attempt = self._mutation_snapshot(phone, operation_id, lease)
            if attempt is None or attempt.owner_token_hash != proof.owner_token_hash:
                raise ConversationMutationPending(FailureReason.MUTATION_PENDING)
            if attempt.phase is MutationPhase.PREPARED:
                return
            if attempt.phase is not MutationPhase.COMMITTING:
                raise ConversationMutationPending(FailureReason.MUTATION_PENDING)
            self._write_attempt(lease, anchor, details, item, replace(attempt, phase=MutationPhase.PREPARED),
                                "restore_prepared", cycle=ConversationCycle.MUTATING)

    def quarantine_ambiguous_commit(self, phone: str, operation_id: str,
                                    lease: ContactLease, now: datetime) -> MutationAttempt:
        with self._lock:
            anchor, details, item, attempt = self._mutation_snapshot(phone, operation_id, lease)
            if attempt is None or attempt.phase is not MutationPhase.COMMITTING:
                raise ConversationMutationPending(FailureReason.MUTATION_PENDING)
            compact = replace(attempt, phase=MutationPhase.QUARANTINED, paused_until=None, reason=None)
            return self._write_attempt(lease, anchor, details, item, compact,
                                       "quarantine_mutation", cycle=ConversationCycle.QUARANTINED, compact=True)

    def finalize_committed(self, phone: str, operation_id: str, lease: ContactLease,
                           now: datetime) -> MutationAttempt:
        with self._lock:
            anchor, details, item, attempt = self._mutation_snapshot(phone, operation_id, lease)
            if (attempt is None or attempt.phase is not MutationPhase.COMMITTING
                    or attempt.owner_token_hash != hashlib.sha256(lease.owner_token.encode()).hexdigest()):
                raise ConversationMutationPending(FailureReason.MUTATION_PENDING)
            return self._write_attempt(lease, anchor, details, item, replace(attempt, phase=MutationPhase.COMMITTED),
                                       "finalize_committed", cycle=attempt.target_cycle)

    def resolve_quarantined_mutation(self, phone: str, operation_id: str, epoch: UUID,
                                     lease: ContactLease, now: datetime, *, quiescent: bool,
                                     outcome: MutationPhase) -> MutationAttempt:
        """Operational assertion of external SQL proof; never called by reads/workers."""
        if (quiescent is not True or epoch != self.config.coordination_epoch
                or outcome not in (MutationPhase.COMMITTED, MutationPhase.ABORTED)):
            raise ConversationStateUnavailable(FailureReason.INVALID_VALUE)
        with self._lock:
            anchor, details, item, attempt = self._mutation_snapshot(phone, operation_id, lease, operational=True)
            if (attempt is None or attempt.epoch != epoch or attempt.operation_id != operation_id
                    or attempt.phase is not MutationPhase.QUARANTINED or anchor.cycle is not ConversationCycle.QUARANTINED):
                raise ConversationStateUnavailable(FailureReason.INVALID_VALUE)
            resolved = replace(attempt, phase=outcome, generation=uuid4(),
                               expected_until=self._now() + timedelta(seconds=self.config.replay_window_seconds))
            return self._write_attempt(lease, anchor, details, item, resolved, "resolve_quarantine",
                                       cycle=attempt.target_cycle if outcome is MutationPhase.COMMITTED else attempt.prior_cycle,
                                       operational=True)

    def renew_lease(self, lease: ContactLease) -> None:
        try:
            with self._lock:
                self.assert_owned(lease)
                keys = contact_keys(lease.phone)
                renewal = {"op": "PEXPIRE", "key": keys.lease,
                           "ttl": self.config.contact_lease_ttl_seconds * 1000}
                if self._get(keys.anchor) is None:
                    checks = [self._lease_check(lease), self._check(keys.anchor, None)]
                    if self._has_remnants(lease.phone):
                        self._atomic(lease.phone, "renew", checks, quarantine=True)
                    self._atomic(lease.phone, "renew", checks, [renewal])
                    return
                anchor, details, checks = self._snapshot(lease)
                renewed = []
                changed = False
                for item in details:
                    if item.entry.kind == "processing" and item.body.get("phase") == "CLAIMED":
                        deadline = min(self._now().timestamp() + self.config.claim_ttl_seconds,
                                       item.body["processing_deadline"])
                        if deadline <= self._now().timestamp():
                            raise ContactLeaseLost(FailureReason.CONTACT_LEASE_LOST)
                        item = replace(item, entry=replace(item.entry, version=item.entry.version + 1),
                                       body={**item.body, "claim_deadline": deadline})
                        changed = True
                    renewed.append(item)
                if changed:
                    self._transition(lease, anchor, tuple(renewed), operation="renew", extra=[renewal])
                else:
                    self._atomic(lease.phone, "renew", checks, [renewal])
        except Exception:
            with self._lock:
                heartbeat = self._heartbeats.get(lease.owner_token)
            if heartbeat is not None:
                heartbeat._lost.set()
            raise ContactLeaseLost(FailureReason.CONTACT_LEASE_LOST) from None
