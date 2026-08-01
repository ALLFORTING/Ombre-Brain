import hashlib
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest
from asset_migration_state import HostMigrationStateError
from remember_me.core import (
    AssetBlobVerificationResult,
    AssetVerificationCompletion,
    AssetVerificationPage,
    AssetVerificationRecord,
    AssetVerificationSnapshot,
    AssetVerificationTag,
)

from remember_me_import_adapter import LegacyAssetImportAdapterError
from remember_me_migration_acceptance import (
    LegacyRmReconciler,
    VERIFICATION_PAGE_SIZE,
)


SNAPSHOT_ID = "s" * 32
CURSOR = "c" * 32
CONTENT = b"x"
CONTENT_SHA = hashlib.sha256(CONTENT).hexdigest()


def _source(asset_id):
    return {
        "asset_id": asset_id,
        "source_sha256": CONTENT_SHA,
        "stored_sha256": CONTENT_SHA,
        "original_filename": f"{asset_id}.png",
        "mime_type": "image/png",
        "kind": "image",
        "decoded_bytes": 1,
        "stored_bytes": 1,
        "width": 1,
        "height": 1,
        "created_at": "2026-07-31T00:00:00+00:00",
        "updated_at": "2026-07-31T00:00:01+00:00",
        "title": "title",
        "description": "description",
        "tags": (
            {
                "value": "tag",
                "created_at": "2026-07-31T00:00:00+00:00",
            },
        ),
    }


def _target(source):
    return AssetVerificationRecord(
        asset_id=source["asset_id"],
        source_sha256=source["source_sha256"],
        stored_sha256=source["stored_sha256"],
        original_filename=source["original_filename"],
        mime_type=source["mime_type"],
        kind=source["kind"],
        decoded_bytes=source["decoded_bytes"],
        stored_bytes=source["stored_bytes"],
        width=source["width"],
        height=source["height"],
        created_at=source["created_at"],
        updated_at=source["updated_at"],
        title=source["title"],
        description=source["description"],
        tags=tuple(
            AssetVerificationTag(**tag) for tag in source["tags"]
        ),
    )


class _State:
    def __init__(self, events=None):
        self.renewals = 0
        self.assertions = 0
        self.events = events

    def renew_freeze(self, owner, *, ttl_seconds):
        assert owner == "owner"
        assert ttl_seconds == 60
        self.renewals += 1
        if self.events is not None:
            self.events.append("renew")

    def assert_freeze_owner(self, owner):
        assert owner == "owner"
        self.assertions += 1
        if self.events is not None:
            self.events.append("assert")


class _Adapter:
    def __init__(
        self,
        sources,
        *,
        fail_phase=None,
        extra=False,
        events=None,
    ):
        self.sources = sources
        self.records = [_target(item) for item in sources.values()]
        if extra:
            self.records.append(_target(_source("f" * 32)))
        self.fail_phase = fail_phase
        self.page_limits = []
        self.verified_ids = []
        self.call_order = []
        self.events = events

    def _fail(self, phase):
        if self.fail_phase == phase:
            raise LegacyAssetImportAdapterError(
                {
                    "begin": "rm_target_verification_snapshot_expired",
                    "page": "rm_target_invalid_verification_cursor",
                    "blob": "rm_target_verification_blob_checksum_mismatch",
                    "complete": "rm_target_verification_snapshot_changed",
                }[phase]
            )

    def begin_target_verification(self):
        self._fail("begin")
        self.call_order.append("begin")
        if self.events is not None:
            self.events.append("begin")
        return AssetVerificationSnapshot(
            SNAPSHOT_ID,
            7,
            len(self.records),
            "a" * 64,
            "image",
        )

    def list_target_verification_page(self, *, snapshot_id, cursor, limit):
        self._fail("page")
        self.call_order.append("page")
        if self.events is not None:
            self.events.append("page")
        assert snapshot_id == SNAPSHOT_ID
        self.page_limits.append(limit)
        start = 0 if cursor == "" else VERIFICATION_PAGE_SIZE
        records = tuple(self.records[start:start + limit])
        has_more = start + len(records) < len(self.records)
        return AssetVerificationPage(
            SNAPSHOT_ID,
            records,
            CURSOR if has_more else "",
            has_more,
            len(self.records),
            7,
        )

    def get_legacy_verification_bytes(self, asset_id):
        assert asset_id in self.sources
        if self.events is not None:
            self.events.append(f"legacy:{asset_id}")
        return CONTENT

    def verify_target_blob(
        self,
        *,
        snapshot_id,
        asset_id,
        expected_sha256,
        expected_size,
        expected_bytes,
    ):
        self._fail("blob")
        self.call_order.append("blob")
        if self.events is not None:
            self.events.append(f"blob:{asset_id}")
        assert snapshot_id == SNAPSHOT_ID
        assert expected_sha256 == CONTENT_SHA
        assert expected_size == 1
        assert expected_bytes == CONTENT
        self.verified_ids.append(asset_id)
        return AssetBlobVerificationResult(
            SNAPSHOT_ID,
            asset_id,
            True,
            CONTENT_SHA,
            1,
            True,
            True,
            True,
            7,
        )

    def complete_target_verification(self, *, snapshot_id):
        self._fail("complete")
        self.call_order.append("complete")
        if self.events is not None:
            self.events.append("complete")
        assert snapshot_id == SNAPSHOT_ID
        count = len(self.verified_ids)
        return AssetVerificationCompletion(
            SNAPSHOT_ID,
            "a" * 64,
            7,
            len(self.records),
            count,
            count,
            0,
            0,
            True,
            True,
        )


def _verify(sources, adapter, state=None):
    reconciler = object.__new__(LegacyRmReconciler)
    reconciler._adapter = adapter
    reconciler._state = state or _State()
    reconciler._lease_ttl_seconds = 60
    reconciler._mismatch_limit = 100
    summary = Counter()
    details = []
    result = reconciler._verify_target(
        sources,
        summary,
        details,
        "owner",
    )
    return result, summary, details


@pytest.mark.parametrize("count", [0, 1, 3, 500, 501])
def test_bounded_verification_pagination_success(count):
    sources = {
        f"{index:032x}": _source(f"{index:032x}")
        for index in range(count)
    }
    state = _State()
    adapter = _Adapter(sources)

    result, summary, details = _verify(sources, adapter, state)

    assert result["error_code"] is None
    assert result["missing"] == 0
    assert result["unexpected"] == 0
    assert result["blob_verified"] == count
    assert summary == Counter()
    assert details == []
    assert adapter.verified_ids == list(sources)
    assert adapter.page_limits == (
        [VERIFICATION_PAGE_SIZE]
        if count <= VERIFICATION_PAGE_SIZE
        else [VERIFICATION_PAGE_SIZE, VERIFICATION_PAGE_SIZE]
    )
    expected_order = ["begin"]
    for start in range(0, max(count, 1), VERIFICATION_PAGE_SIZE):
        expected_order.append("page")
        expected_order.extend(
            ["blob"] * min(VERIFICATION_PAGE_SIZE, max(count - start, 0))
        )
    expected_order.append("complete")
    assert adapter.call_order == expected_order
    expected_pages = 1 if count <= VERIFICATION_PAGE_SIZE else 2
    assert state.renewals == expected_pages + count
    assert state.assertions == count + 1


def test_each_blob_renews_then_checks_freeze_ownership():
    sources = {
        f"{index:032x}": _source(f"{index:032x}")
        for index in range(3)
    }
    events = []
    state = _State(events)
    adapter = _Adapter(sources, events=events)

    result, summary, _ = _verify(sources, adapter, state)

    assert result["error_code"] is None
    assert summary == Counter()
    expected = ["begin", "renew", "page"]
    for asset_id in sources:
        expected.extend(
            [
                "renew",
                f"legacy:{asset_id}",
                f"blob:{asset_id}",
                "assert",
            ]
        )
    expected.extend(["complete", "assert"])
    assert events == expected


def test_lease_loss_after_blob_stops_before_next_asset_or_completion():
    sources = {
        f"{index:032x}": _source(f"{index:032x}")
        for index in range(3)
    }

    class LeaseLosingState(_State):
        def assert_freeze_owner(self, owner):
            super().assert_freeze_owner(owner)
            if self.assertions == 2:
                raise HostMigrationStateError("migration_freeze_lost")

    state = LeaseLosingState()
    adapter = _Adapter(sources)

    with pytest.raises(
        HostMigrationStateError,
        match="^migration_freeze_lost$",
    ):
        _verify(sources, adapter, state)

    assert adapter.verified_ids == list(sources)[:2]
    assert adapter.call_order.count("blob") == 2
    assert "complete" not in adapter.call_order


def test_metadata_tags_and_exact_bytes_are_compared():
    sources = {"0" * 32: _source("0" * 32)}
    adapter = _Adapter(sources)
    sources["0" * 32]["title"] = "changed"
    sources["0" * 32]["tags"] = ({
        "value": "changed",
        "created_at": "2026-07-31T00:00:00+00:00",
    },)

    result, summary, _ = _verify(sources, adapter)

    assert result["error_code"] is None
    assert result["blob_verified"] == 1
    assert summary == Counter({"title_mismatch": 1, "tags_mismatch": 1})


def test_unexpected_inventory_fails_closed_without_verifying_unknown_blob():
    sources = {"0" * 32: _source("0" * 32)}
    result, summary, _ = _verify(
        sources,
        _Adapter(sources, extra=True),
    )

    assert result["error_code"] == "target_verification_incomplete"
    assert result["unexpected"] == 1
    assert summary["target_inventory_count_mismatch"] == 1
    assert summary["unexpected_target_asset"] == 1


def test_missing_inventory_fails_closed_before_completion():
    sources = {
        "0" * 32: _source("0" * 32),
        "1" * 32: _source("1" * 32),
    }
    adapter = _Adapter(sources)
    adapter.records.pop()

    result, summary, _ = _verify(sources, adapter)

    assert result["error_code"] == "target_verification_incomplete"
    assert result["missing"] == 1
    assert summary == Counter({"target_inventory_count_mismatch": 1})
    assert "complete" not in adapter.call_order


def test_duplicate_asset_identity_fails_closed():
    sources = {
        "0" * 32: _source("0" * 32),
        "1" * 32: _source("1" * 32),
    }
    adapter = _Adapter(sources)
    adapter.records[1] = adapter.records[0]

    result, _, _ = _verify(sources, adapter)

    assert result["error_code"] == "target_verification_duplicate_asset"
    assert result["blob_verified"] == 1
    assert "complete" not in adapter.call_order


def test_cursor_cycle_fails_closed():
    sources = {
        f"{index:032x}": _source(f"{index:032x}")
        for index in range(1001)
    }
    adapter = _Adapter(sources)
    original_page = adapter.list_target_verification_page

    def cycling_page(*, snapshot_id, cursor, limit):
        page = original_page(
            snapshot_id=snapshot_id,
            cursor=cursor,
            limit=limit,
        )
        if cursor == CURSOR:
            return replace(page, next_cursor=CURSOR, has_more=True)
        return page

    adapter.list_target_verification_page = cycling_page
    result, _, _ = _verify(sources, adapter)

    assert result["error_code"] == "target_verification_cursor_invalid"
    assert "complete" not in adapter.call_order


def test_short_nonterminal_page_fails_closed():
    sources = {
        f"{index:032x}": _source(f"{index:032x}")
        for index in range(501)
    }
    adapter = _Adapter(sources)
    original_page = adapter.list_target_verification_page

    def short_page(*, snapshot_id, cursor, limit):
        page = original_page(
            snapshot_id=snapshot_id,
            cursor=cursor,
            limit=limit,
        )
        if cursor == "":
            return replace(page, records=page.records[:-1])
        return page

    adapter.list_target_verification_page = short_page
    result, _, _ = _verify(sources, adapter)

    assert result["error_code"] == "target_verification_result_invalid"
    assert adapter.verified_ids == []


@pytest.mark.parametrize(
    ("change", "expected_code"),
    [
        ({"readable": False}, "target_verification_result_invalid"),
        (
            {"matches_expected_sha256": False},
            "target_verification_result_invalid",
        ),
        (
            {"matches_expected_size": False},
            "target_verification_result_invalid",
        ),
        (
            {"matches_expected_bytes": False},
            "target_verification_result_invalid",
        ),
        ({"actual_sha256": "f" * 64}, "target_verification_result_invalid"),
        ({"actual_size": 2}, "target_verification_result_invalid"),
        ({"generation": 8}, "target_verification_result_invalid"),
    ],
)
def test_blob_result_must_prove_exact_content(change, expected_code):
    sources = {"0" * 32: _source("0" * 32)}
    adapter = _Adapter(sources)
    original_verify = adapter.verify_target_blob

    def changed_result(**kwargs):
        return replace(original_verify(**kwargs), **change)

    adapter.verify_target_blob = changed_result
    result, _, _ = _verify(sources, adapter)

    assert result["error_code"] == expected_code
    assert "complete" not in adapter.call_order


@pytest.mark.parametrize(
    "change",
    [
        {"target_identity": "b" * 64},
        {"generation": 8},
        {"scanned_count": 0},
        {"blob_verified_count": 0},
        {"duplicate_asset_count": 1},
        {"duplicate_stored_sha_count": 1},
        {"unchanged": False},
        {"complete": False},
    ],
)
def test_completion_must_attest_fresh_integrity(change):
    sources = {"0" * 32: _source("0" * 32)}
    adapter = _Adapter(sources)
    original_complete = adapter.complete_target_verification

    def changed_completion(**kwargs):
        return replace(original_complete(**kwargs), **change)

    adapter.complete_target_verification = changed_completion
    result, _, _ = _verify(sources, adapter)

    assert result["error_code"] == "target_verification_completion_invalid"


@pytest.mark.parametrize(
    ("phase", "code"),
    [
        ("begin", "target_verification_snapshot_expired"),
        ("page", "target_invalid_verification_cursor"),
        ("blob", "target_verification_blob_checksum_mismatch"),
        ("complete", "target_verification_snapshot_changed"),
    ],
)
def test_public_verification_errors_are_redacted(phase, code):
    sources = {"0" * 32: _source("0" * 32)}
    result, _, _ = _verify(sources, _Adapter(sources, fail_phase=phase))

    assert result["error_code"] == code
    serialized = repr(result)
    assert SNAPSHOT_ID not in serialized
    assert CURSOR not in serialized
    assert repr(CONTENT) not in serialized


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit()])
def test_base_exceptions_are_not_mapped(error):
    sources = {"0" * 32: _source("0" * 32)}
    adapter = _Adapter(sources)

    def interrupt():
        raise error

    adapter.begin_target_verification = interrupt
    with pytest.raises(type(error)):
        _verify(sources, adapter)


def test_acceptance_uses_only_public_verification_bridge():
    root = Path(__file__).resolve().parents[1]
    acceptance = (
        root / "remember_me_migration_acceptance.py"
    ).read_text(encoding="utf-8")
    adapter = (
        root / "remember_me_import_adapter.py"
    ).read_text(encoding="utf-8")

    for method in (
        "begin_target_verification",
        "list_target_verification_page",
        "verify_target_blob",
        "complete_target_verification",
    ):
        assert method in acceptance
        assert method in adapter
    assert "self._core" in adapter
    assert "sqlite3" not in acceptance
    assert "stored_relpath" not in acceptance
    assert ".search(" not in acceptance
    assert "reindex" not in acceptance.lower()
