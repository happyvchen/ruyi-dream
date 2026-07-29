from __future__ import annotations

import pytest

from app import object_storage


def test_shared_direct_upload_scope_cannot_be_deleted_as_a_prefix(monkeypatch) -> None:
    monkeypatch.setattr(
        object_storage,
        "s3_client",
        lambda: (_ for _ in ()).throw(AssertionError("storage client must not be reached")),
    )

    with pytest.raises(ValueError, match="共享对象前缀"):
        object_storage.delete_prefix("direct-uploads/styling-garment")


@pytest.mark.parametrize(
    "prefix",
    [
        "generation-tasks/42",
        "styling-garments/look-1",
        "uploads/batch-1",
        "outputs/task-1",
        "direct-uploads/styling-garment/session-1",
        "media/deadbeefdeadbeefdeadbeefdeadbeef",
    ],
)
def test_only_model_profile_owner_directories_allow_prefix_deletion(prefix, monkeypatch) -> None:
    monkeypatch.setattr(
        object_storage,
        "s3_client",
        lambda: (_ for _ in ()).throw(AssertionError("storage client must not be reached")),
    )

    with pytest.raises(ValueError, match="共享对象前缀"):
        object_storage.delete_prefix(prefix)


def test_specific_model_profile_owner_directory_allows_prefix_deletion(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    class Paginator:
        def paginate(self, *, Bucket, Prefix):
            calls.append((Bucket, Prefix))
            return []

    class Client:
        def get_paginator(self, name):
            assert name == "list_objects_v2"
            return Paginator()

    monkeypatch.setattr(object_storage, "s3_client", Client)

    object_storage.delete_prefix("model-profiles/model-1")

    assert calls == [(object_storage.get_settings().s3_bucket, "model-profiles/model-1/")]
