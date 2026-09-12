"""Task 2 contracts; all coordination runs against deterministic script semantics."""

from dataclasses import replace
from datetime import timedelta
from threading import Barrier, Thread
from uuid import UUID
import json

import pytest

from app.conversation_state import (
    ConfigurationIssue, ContactLeaseLost, ContactLockUnavailable, ConversationGenerationUnavailable,
    ConversationStateUnavailable, ReadinessUnavailable,
)

PHONE = "5551999990000"
OTHER = "5551888880000"


def make_store():
    from tests.fakes import InMemoryConversationStore
    from tests.test_conversation_state import _valid_environment
    from app.conversation_state import ConversationConfig
    from app.simple_config import Settings
    return InMemoryConversationStore(ConversationConfig.from_settings(Settings(_valid_environment())))


def detail(store, kind="batch", flags=("dispatch",), terminal=False):
    from app.conversation_state import ContactDetail, ManifestEntry
    return ContactDetail(ManifestEntry(kind, "item-1", 1,
                         store.clock.now() + timedelta(seconds=120), flags),
                         {"phase": "DONE" if terminal else "PENDING"}, terminal)


@pytest.mark.parametrize("fault", ["epoch_absent", "epoch_mismatch", "run_id_mismatch", "noeviction_invalid", "persistence_invalid"])
def test_readiness_fails_closed_for_each_coordination_fault(fault):
    """Catches accepting an untrusted Redis incarnation or configuration."""
    store = make_store()
    assert store.readiness().ready
    store.inject_fault(fault)
    result = store.readiness()
    assert result.ready is False
    assert result.components["redis"] != "ready"


def test_store_never_initializes_epoch_implicitly():
    """Catches self-healing a missing global fence during initialization."""
    store = make_store()
    store.delete_global_epoch()
    with pytest.raises(ReadinessUnavailable):
        store.initialize_contact("5551999990000")
    assert store.global_epoch_writes == 0


def test_anchor_initialization_requires_explicit_empty_database_evidence():
    """Catches silently reconstructing lost Redis state over existing SQL state."""
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        for evidence in (None, True):
            with pytest.raises(ConversationGenerationUnavailable):
                store.initialize_contact(PHONE, lease, db_state_present=evidence)
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        assert anchor.contact_revision == 0
        assert isinstance(anchor.last_generation, UUID)
        assert store.read_anchor(lease) == anchor


@pytest.mark.parametrize("kind,flags", [("batch", ("dispatch",)), ("buffer", ()), ("staging", ("staging",)), ("processing", ()), ("mutation", ()), ("dedupe", ())])
def test_manifest_missing_detail_quarantines_only_affected_contact(kind, flags):
    """Catches missing referenced state being silently recreated or poisoning peers."""
    store = make_store()
    with store.contact_lease(PHONE) as lease, store.contact_lease(OTHER) as other:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        peer = store.initialize_contact(OTHER, other, db_state_present=False)
        item = detail(store, kind, flags)
        store.compare_and_set(lease, anchor, (item,))
        store.delete_detail(PHONE, item.entry)
        with pytest.raises(ConversationGenerationUnavailable):
            store.read_anchor(lease)
        assert store.is_quarantined(PHONE)
        assert store.read_anchor(other) == peer


@pytest.mark.parametrize("fault", ["dispatch", "staging", "fingerprint", "manifest", "generation"])
def test_manifest_divergence_quarantines_contact(fault):
    """Catches accepting missing indices, malformed anchors, or generation divergence."""
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        store.compare_and_set(lease, anchor, (detail(store, flags=("dispatch", "staging")),))
        store.corrupt_contact(PHONE, fault)
        with pytest.raises(ConversationGenerationUnavailable):
            store.read_anchor(lease)
        assert store.is_quarantined(PHONE)


def test_anchor_stale_revision_and_fingerprint_never_write():
    """Catches stale callers overwriting newer complete state."""
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        initial = store.initialize_contact(PHONE, lease, db_state_present=False)
        current = store.compare_and_set(lease, initial, (detail(store),))
        for stale in (initial, replace(current, manifest_fingerprint="bad")):
            before = store.snapshot()
            with pytest.raises(ConversationStateUnavailable):
                store.compare_and_set(lease, stale, ())
            assert store.snapshot() == before
        assert store.read_anchor(lease) == current


def test_anchor_generation_cannot_reuse_an_earlier_uuid():
    """Catches an old generation becoming valid again after a transition."""
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        old = store.initialize_contact(PHONE, lease, db_state_present=False)
        current = store.compare_and_set(lease, old, (), generation=UUID(int=2))
        with pytest.raises(ConversationGenerationUnavailable):
            store.compare_and_set(lease, current, (), generation=old.last_generation)
        assert store.is_quarantined(PHONE)


def test_manifest_atomic_failure_preserves_all_keys_indices_revision():
    """Catches partial detail/index/anchor writes on a failed CAS."""
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        before = store.snapshot()
        store.fail_next_atomic("cas")
        with pytest.raises(ConversationStateUnavailable):
            store.compare_and_set(lease, anchor, (detail(store),))
        unchanged = store.snapshot() == before
        assert unchanged


def test_manifest_cleanup_removes_terminal_details_only_at_horizon():
    """Catches early removal and expired terminal details being treated as loss."""
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        item = detail(store, terminal=True)
        anchor = store.compare_and_set(lease, anchor, (item,))
        assert store.cleanup(lease, anchor) == anchor
        for _ in range(6):
            store.clock.advance(timedelta(seconds=20))
            store.renew_lease(lease)
        cleaned = store.cleanup(lease, store.read_anchor(lease))
        assert cleaned.manifest == ()
        assert cleaned.contact_revision > anchor.contact_revision


def test_lease_expired_owner_cannot_release_or_mutate_successor():
    """Catches stale-owner release deleting a successor's lease or allowing writes."""
    store = make_store()
    with store.contact_lease(PHONE) as first:
        anchor = store.initialize_contact(PHONE, first, db_state_present=False)
        store.clock.advance(timedelta(seconds=61))
        with store.contact_lease(PHONE) as second:
            store.release_lease(first)
            store.assert_owned(second)
            with pytest.raises(ContactLeaseLost):
                store.compare_and_set(first, anchor, ())
            assert store.read_anchor(second) == anchor


def test_lease_two_owners_barrier_loser_changes_no_state():
    """Catches SET without NX allowing simultaneous owners to mutate a contact."""
    store = make_store()
    with store.contact_lease(PHONE) as initial_lease:
        store.initialize_contact(PHONE, initial_lease, db_state_present=False)
    start, held = Barrier(2), Barrier(2)
    outcomes = []
    def contender():
        start.wait(timeout=2)
        try:
            with store.contact_lease(PHONE) as lease:
                anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
                held.wait(timeout=2)
                outcomes.append(("won", anchor.contact_revision))
        except ContactLockUnavailable:
            before = store.contact_snapshot(PHONE)
            held.wait(timeout=2)
            outcomes.append(("lost", before == store.contact_snapshot(PHONE)))
    threads = [Thread(target=contender) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert sorted(outcomes) == [("lost", True), ("won", 0)]


def test_lease_heartbeat_renews_at_one_third_ttl_and_stops():
    """Catches delayed renewal, leaked heartbeat workers, and swallowed lease loss."""
    from tests.fakes import ControlledWait
    store = make_store()
    waiter = ControlledWait(store.clock)
    store.heartbeat_wait = waiter
    with store.contact_lease(PHONE) as lease:
        waiter.tick(19)
        assert store.lease_remaining(PHONE) == 41
        waiter.tick(1)
        store.assert_owned(lease)
        assert store.lease_remaining(PHONE) == 60
        store.fail_next_atomic("renew")
        waiter.tick(20)
        with pytest.raises(ContactLeaseLost):
            lease.assert_owned()
    assert waiter.stopped


def test_lease_claim_renewal_is_atomic_and_bounded_by_processing_deadline():
    """Catches renewal extending a claim past its processing horizon or stale manifest."""
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        item = detail(store, "processing", ())
        body = {"phase": "CLAIMED", "claim_deadline": store.clock.now().timestamp() + 10,
                "processing_deadline": store.clock.now().timestamp() + 30}
        anchor = store.compare_and_set(lease, anchor, (replace(item, body=body),))
        store.clock.advance(timedelta(seconds=20))
        store.renew_lease(lease)
        current = store.read_anchor(lease)
        renewed = store.read_details(lease)[0]
        assert renewed.body["claim_deadline"] == body["processing_deadline"]
        assert renewed.entry.version == 2
        assert current.contact_revision == anchor.contact_revision + 1
        assert current.manifest_fingerprint != anchor.manifest_fingerprint


def test_epoch_change_between_readiness_and_cas_blocks_mutation():
    """Catches relying on stale readiness after the global epoch changes."""
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        store.before_atomic = lambda: store.inject_fault("epoch_mismatch")
        with pytest.raises(ReadinessUnavailable):
            store.compare_and_set(lease, anchor, ())
        assert store.contact_snapshot(PHONE)["revision"] == anchor.contact_revision


def test_anchor_missing_with_surviving_details_quarantines_contact():
    """Catches an absent anchor being treated as a clean contact despite remnants."""
    from app.conversation_redis import contact_keys
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        store.compare_and_set(lease, anchor, (detail(store),))
        store.client.values.pop(contact_keys(PHONE).anchor)
        with pytest.raises(ConversationGenerationUnavailable):
            store.read_anchor(lease)
        assert store.is_quarantined(PHONE)


def test_anchor_initialization_rejects_orphan_global_index_membership():
    """Catches initialization after partial loss leaves only a global index remnant."""
    from app.conversation_redis import contact_keys
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        store.compare_and_set(lease, anchor, (detail(store),))
        keys = contact_keys(PHONE)
        for key in list(store.client.values):
            if key.startswith(keys.anchor.rsplit(":", 1)[0]) and key != keys.lease:
                store.client.values.pop(key)
        with pytest.raises(ConversationGenerationUnavailable):
            store.initialize_contact(PHONE, lease, db_state_present=False)
        assert store.is_quarantined(PHONE)


def test_manifest_update_rejects_regressive_or_unchanged_detail_version():
    """Catches changing a detail while reusing the old manifest version fence."""
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        item = detail(store)
        anchor = store.compare_and_set(lease, anchor, (item,))
        with pytest.raises(ConversationGenerationUnavailable):
            store.compare_and_set(lease, anchor, (replace(item, body={"phase": "STAGED"}),))
        assert store.is_quarantined(PHONE)


def test_lease_invalid_configuration_fails_with_domain_reason_before_acquire():
    """Catches invalid TTL arithmetic escaping instead of readiness failing closed."""
    store = make_store()
    store.config = replace(store.config, contact_lease_ttl_seconds=None,
                           issues=(ConfigurationIssue.INVALID_CONTACT_LEASE_TTL,))
    with pytest.raises(ReadinessUnavailable):
        with store.contact_lease(PHONE):
            pytest.fail("invalid configuration acquired a lease")


@pytest.mark.parametrize("quarantine", [False, True])
def test_manifest_acl_denial_of_later_write_keeps_entire_state_unchanged(quarantine):
    """Catches a denied later SADD leaving earlier SET/DEL writes committed."""
    from app.conversation_redis import DISPATCH_INDEX_KEY, QUARANTINE_INDEX_KEY
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        target = QUARANTINE_INDEX_KEY if quarantine else DISPATCH_INDEX_KEY
        store.client.denied_commands.add(("SADD", target))
        if quarantine:
            store.corrupt_contact(PHONE, "manifest")
        before = store.snapshot()
        with pytest.raises(ConversationStateUnavailable):
            if quarantine:
                store.read_anchor(lease)
            else:
                store.compare_and_set(lease, anchor, (detail(store),))
        unchanged = store.snapshot() == before
        assert unchanged


@pytest.mark.parametrize("target", ["anchor", "control"])
def test_anchor_regressive_revision_quarantines_only_that_contact(target):
    """Catches accepting a rolled-back anchor revision after a completed CAS."""
    from app.conversation_redis import RedisConversationStore, contact_keys
    store = make_store()
    with store.contact_lease(PHONE) as lease, store.contact_lease(OTHER) as peer_lease:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        peer = store.initialize_contact(OTHER, peer_lease, db_state_present=False)
        # Include an empty manifest: checking detail versions alone cannot fence it.
        updated = store.compare_and_set(lease, anchor, ())
        assert updated.contact_revision == 1
        key = contact_keys(PHONE).anchor if target == "anchor" else contact_keys(PHONE).generation
        raw = store.client.values[key]
        if not raw.startswith("{"):
            pytest.fail("generation control does not retain a durable revision fence")
        corrupt = json.loads(raw)
        corrupt["contact_revision"] = 0
        store.client.values[key] = json.dumps(corrupt)
        assert store.read_anchor(peer_lease) == peer
    # A fresh store/lease must detect the regression without process-local memory.
    restarted = RedisConversationStore(store.client, store.config, store.clock)
    with restarted.contact_lease(PHONE) as lease:
        with pytest.raises(ConversationGenerationUnavailable):
            restarted.read_anchor(lease)
        assert store.is_quarantined(PHONE)
    with store.contact_lease(OTHER) as peer_lease:
        assert store.read_anchor(peer_lease) == peer


@pytest.mark.parametrize("ttl", [-1, True, 2 ** 63])
def test_manifest_invalid_late_write_argument_preserves_all_state(ttl):
    """Catches invalid TTL in a later operation failing after a prior SET."""
    from app.conversation_redis import contact_keys
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        store.initialize_contact(PHONE, lease, db_state_present=False)
        keys = contact_keys(PHONE)
        before = store.snapshot()
        with pytest.raises(ConversationStateUnavailable):
            store._atomic(PHONE, "cas", [store._lease_check(lease)], [
                {"op": "SET", "key": keys.buffer_prefix + "item-1", "value": "synthetic"},
                {"op": "PEXPIRE", "key": keys.lease, "ttl": ttl},
            ])
        unchanged = store.snapshot() == before
        assert unchanged


def test_readiness_missing_acl_preflight_capability_blocks_acquire_without_writes():
    """Catches mutating on Redis that cannot authorize the entire script write batch."""
    store = make_store()
    store.client.acl_check_available = False
    before = store.snapshot()
    with pytest.raises(ConversationStateUnavailable):
        with store.contact_lease(PHONE):
            pytest.fail("missing ACL preflight capability acquired a lease")
    unchanged = store.snapshot() == before
    assert unchanged


@pytest.mark.parametrize("remnant", ["generation", "detail", "index"])
def test_lease_renewal_after_partial_anchor_loss_quarantines_and_loses_ownership(remnant):
    """Catches missing-anchor renewal prolonging permission to mutate lost state."""
    from app.conversation_redis import contact_keys
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        item = detail(store)
        store.compare_and_set(lease, anchor, (item,))
        keys = contact_keys(PHONE)
        for key in list(store.client.values):
            if key.startswith(keys.anchor.rsplit(":", 1)[0]) and key != keys.lease:
                if remnant == "generation" and key == keys.generation:
                    continue
                if remnant == "detail" and key == store._detail_key(PHONE, item.entry):
                    continue
                store.client.values.pop(key)
        if remnant != "index":
            store.client.sets.clear()
        with pytest.raises(ContactLeaseLost):
            store.renew_lease(lease)
        assert store.is_quarantined(PHONE)
        with pytest.raises(ContactLeaseLost):
            lease.assert_owned()


@pytest.mark.parametrize("field,value", [("last_generation", 3), ("last_generation", {}),
                                         ("generation_history", [3]),
                                         ("generation_history", {"invalid": 1})])
def test_anchor_corrupt_uuid_json_types_quarantine_with_domain_reason(field, value):
    """Catches UUID parser AttributeError escaping without quarantining corrupt state."""
    from app.conversation_redis import contact_keys
    from app.conversation_state import FailureReason
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        store.initialize_contact(PHONE, lease, db_state_present=False)
        key = contact_keys(PHONE).anchor
        anchor = json.loads(store.client.values[key])
        anchor[field] = value
        store.client.values[key] = json.dumps(anchor)
        with pytest.raises(ConversationGenerationUnavailable) as error:
            store.read_anchor(lease)
        assert error.value.reason_code is FailureReason.GENERATION_UNAVAILABLE
        assert store.is_quarantined(PHONE)
