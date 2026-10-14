from __future__ import annotations

import asyncio
import traceback
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from engagemate.domain.budgets import ActionBudget
from engagemate.worker.discovery import (
    DiscoveryError,
    DiscoveryService,
)
from engagemate.worker.story_types import (
    ActiveStory,
    DiscoveryContext,
    DiscoveryHistory,
    DiscoveryTarget,
    StoryContent,
    StoryMedia,
    StoryMediaType,
    StoryOpportunity,
    TargetEngagement,
)

NOW = datetime(2026, 7, 31, 8, 0, tzinfo=UTC)
USER_ID = UUID("11111111-1111-4111-8111-111111111111")
ACCOUNT_ID = UUID("22222222-2222-4222-8222-222222222222")
SECRET = "dependency-password"
CUSTOM_SECRET = "CUSTOM-OBJECT-SECRET"
JPEG = StoryMedia(mime_type="image/jpeg", data=b"\xff\xd8story-image")
IMAGE_CONTENT = StoryContent(
    media_type=StoryMediaType.IMAGE,
    caption_text="Launch day",
    sticker_text=None,
    image=JPEG,
    video_frames=(),
)


class SecretObject:
    def __repr__(self) -> str:
        return CUSTOM_SECRET


class EvilDate(datetime):
    def astimezone(self, tz: object = None) -> EvilDate:
        return self

    def __repr__(self) -> str:
        return CUSTOM_SECRET


def target(
    suffix: int,
    *,
    tier: str = "broad",
    enabled: bool = True,
    user_id: UUID = USER_ID,
    account_id: UUID = ACCOUNT_ID,
) -> DiscoveryTarget:
    return DiscoveryTarget(
        id=UUID(f"30000000-0000-4000-8000-{suffix:012d}"),
        user_id=user_id,
        ig_account_id=account_id,
        owner_id=f"owner-{suffix}",
        handle=f"target_{suffix}",
        tier=tier,
        enabled=enabled,
    )


def story(
    target_value: DiscoveryTarget,
    suffix: int,
    *,
    taken_at: datetime = NOW - timedelta(hours=1),
    media_id: str | None = None,
    owner_id: str | None = None,
) -> ActiveStory:
    return ActiveStory(
        target_id=target_value.id,
        owner_id=owner_id or target_value.owner_id,
        media_id=media_id or f"media-{suffix}",
        taken_at=taken_at,
        media_hash=None,
        content=IMAGE_CONTENT,
    )


class FakeAdapter:
    def __init__(
        self,
        stories: dict[UUID, tuple[ActiveStory, ...]],
        error: BaseException | None = None,
    ) -> None:
        self.stories = stories
        self.error = error
        self.calls: list[tuple[DiscoveryContext, DiscoveryTarget]] = []

    async def active_stories(
        self, context: DiscoveryContext, target_value: DiscoveryTarget
    ) -> tuple[ActiveStory, ...]:
        self.calls.append((context, target_value))
        if self.error is not None:
            raise self.error
        return self.stories.get(target_value.id, ())


class FakeHistoryRepository:
    def __init__(
        self,
        result: DiscoveryHistory | object | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.result = result or DiscoveryHistory(frozenset(), ())
        self.error = error
        self.calls: list[tuple[UUID, UUID, frozenset[str], frozenset[UUID]]] = []

    async def lookup(
        self,
        user_id: UUID,
        ig_account_id: UUID,
        fingerprints: frozenset[str],
        target_ids: frozenset[UUID],
    ) -> DiscoveryHistory:
        self.calls.append((user_id, ig_account_id, fingerprints, target_ids))
        if self.error is not None:
            raise self.error
        return self.result  # type: ignore[return-value]


def service(
    stories: dict[UUID, tuple[ActiveStory, ...]],
    history: DiscoveryHistory | object | None = None,
) -> tuple[DiscoveryService, FakeAdapter, FakeHistoryRepository]:
    adapter = FakeAdapter(stories)
    repository = FakeHistoryRepository(history)
    return DiscoveryService(adapter, repository), adapter, repository


@pytest.mark.asyncio
async def test_find_short_circuits_when_no_shared_or_action_capacity() -> None:
    enabled = target(1)
    for budget in (
        ActionBudget(likes=4, replies=2, dms=1, plan_total=0),
        ActionBudget(likes=0, replies=0, dms=0, plan_total=10),
    ):
        discovery, adapter, repository = service({enabled.id: (story(enabled, 1),)})

        assert (
            await discovery.find(
                DiscoveryContext(USER_ID, ACCOUNT_ID, NOW), (enabled,), budget
            )
            == []
        )
        assert adapter.calls == []
        assert repository.calls == []


@pytest.mark.asyncio
async def test_find_excludes_disabled_targets_before_browser_calls() -> None:
    enabled = target(1)
    disabled = target(2, enabled=False)
    discovery, adapter, repository = service(
        {
            enabled.id: (story(enabled, 1),),
            disabled.id: (story(disabled, 2),),
        }
    )

    result = await discovery.find(
        DiscoveryContext(USER_ID, ACCOUNT_ID, NOW),
        (disabled, enabled),
        ActionBudget(1, 0, 0, 1),
    )

    assert [item.target_id for item in result] == [enabled.id]
    assert [call[1].id for call in adapter.calls] == [enabled.id]
    assert repository.calls[0][3] == frozenset({enabled.id})


@pytest.mark.asyncio
async def test_find_applies_story_age_boundaries_and_future_filter() -> None:
    current = target(1)
    discovery, _, repository = service(
        {
            current.id: (
                story(
                    current,
                    1,
                    taken_at=NOW - timedelta(hours=24) + timedelta(microseconds=1),
                ),
                story(current, 2, taken_at=NOW - timedelta(hours=24)),
                story(current, 3, taken_at=NOW - timedelta(hours=24, microseconds=1)),
                story(current, 4, taken_at=NOW + timedelta(microseconds=1)),
            )
        }
    )

    result = await discovery.find(
        DiscoveryContext(USER_ID, ACCOUNT_ID, NOW),
        (current,),
        ActionBudget(10, 0, 0, 10),
    )

    assert [item.media_id for item in result] == ["media-1"]
    assert len(repository.calls[0][2]) == 1


@pytest.mark.asyncio
async def test_find_deduplicates_batch_and_uses_one_tenant_scoped_history_query() -> (
    None
):
    current = target(1)
    repeated = story(current, 1, media_id="same-media")
    discovery, _, repository = service({current.id: (repeated, repeated)})

    result = await discovery.find(
        DiscoveryContext(USER_ID, ACCOUNT_ID, NOW),
        (current,),
        ActionBudget(4, 0, 0, 4),
    )

    assert len(result) == 1
    assert result[0].target_id == current.id
    assert len(repository.calls) == 1
    assert repository.calls[0][0:2] == (USER_ID, ACCOUNT_ID)
    assert len(repository.calls[0][2]) == 1
    assert repository.calls[0][3] == frozenset({current.id})


@pytest.mark.asyncio
async def test_find_excludes_previously_engaged_fingerprints() -> None:
    current = target(1)
    first_story = story(current, 1)
    preliminary, _, preliminary_repository = service({current.id: (first_story,)})
    first_result = await preliminary.find(
        DiscoveryContext(USER_ID, ACCOUNT_ID, NOW),
        (current,),
        ActionBudget(2, 0, 0, 2),
    )
    seen = first_result[0].fingerprint
    discovery, _, _ = service(
        {current.id: (first_story, story(current, 2))},
        DiscoveryHistory(frozenset({seen}), ()),
    )

    result = await discovery.find(
        DiscoveryContext(USER_ID, ACCOUNT_ID, NOW),
        (current,),
        ActionBudget(2, 0, 0, 2),
    )

    assert [item.media_id for item in result] == ["media-2"]
    assert len(preliminary_repository.calls) == 1


@pytest.mark.asyncio
async def test_find_ranks_tiers_history_and_ties_independently_of_adapter_order() -> (
    None
):
    vip_old = target(1, tier="vip")
    vip_never_b = target(2, tier="vip")
    broad_never = target(3, tier="broad")
    vip_never_a = target(4, tier="vip")
    discovery_old = target(5, tier="discovery")
    vip_recent = target(6, tier="vip")
    targets = (
        discovery_old,
        vip_recent,
        broad_never,
        vip_never_b,
        vip_old,
        vip_never_a,
    )
    history = DiscoveryHistory(
        frozenset(),
        (
            TargetEngagement(vip_recent.id, NOW - timedelta(days=1)),
            TargetEngagement(discovery_old.id, NOW - timedelta(days=30)),
            TargetEngagement(vip_old.id, NOW - timedelta(days=10)),
        ),
    )
    stories = {
        item.id: (
            story(item, 2, media_id=f"z-{item.id}"),
            story(item, 1, media_id=f"a-{item.id}"),
        )
        for item in targets
    }
    discovery, _, _ = service(stories, history)

    result = await discovery.find(
        DiscoveryContext(USER_ID, ACCOUNT_ID, NOW),
        targets,
        ActionBudget(20, 0, 0, 20),
    )

    ordered_targets = [item.target_id for item in result]
    assert ordered_targets[::2] == [
        vip_never_b.id,
        vip_never_a.id,
        vip_old.id,
        vip_recent.id,
        broad_never.id,
        discovery_old.id,
    ]
    assert [item.media_id for item in result[0:2]] == [
        f"a-{vip_never_b.id}",
        f"z-{vip_never_b.id}",
    ]


@pytest.mark.asyncio
async def test_find_truncates_to_actual_usable_shared_capacity() -> None:
    current = target(1)
    discovery, _, _ = service(
        {current.id: tuple(story(current, index) for index in range(10))}
    )

    result = await discovery.find(
        DiscoveryContext(USER_ID, ACCOUNT_ID, NOW),
        (current,),
        ActionBudget(likes=1, replies=2, dms=1, plan_total=9),
    )

    assert len(result) == 4


@pytest.mark.parametrize(
    ("context", "targets", "budget"),
    [
        (object(), (), ActionBudget(1, 0, 0, 1)),
        (
            DiscoveryContext(USER_ID, ACCOUNT_ID, NOW.replace(tzinfo=None)),
            (),
            ActionBudget(1, 0, 0, 1),
        ),
        (
            DiscoveryContext(USER_ID, ACCOUNT_ID, NOW),
            (target(1, tier="unknown"),),
            ActionBudget(1, 0, 0, 1),
        ),
        (
            DiscoveryContext(USER_ID, ACCOUNT_ID, NOW),
            (target(1), target(1)),
            ActionBudget(1, 0, 0, 1),
        ),
        (
            DiscoveryContext(USER_ID, ACCOUNT_ID, NOW),
            (target(1, user_id=uuid4()),),
            ActionBudget(1, 0, 0, 1),
        ),
        (
            DiscoveryContext(USER_ID, ACCOUNT_ID, NOW),
            (target(1, account_id=uuid4()),),
            ActionBudget(1, 0, 0, 1),
        ),
        (DiscoveryContext(USER_ID, ACCOUNT_ID, NOW), (), object()),
    ],
)
@pytest.mark.asyncio
async def test_find_rejects_invalid_context_targets_and_budget(
    context: object, targets: object, budget: object
) -> None:
    discovery, _, _ = service({})

    with pytest.raises(DiscoveryError, match="^invalid discovery input$"):
        await discovery.find(context, targets, budget)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad_story",
    [
        object(),
        ActiveStory(uuid4(), "owner", "media", NOW, None, IMAGE_CONTENT),
        ActiveStory(
            target(1).id,
            "different-owner",
            "media",
            NOW,
            None,
            IMAGE_CONTENT,
        ),
        ActiveStory(
            target(1).id,
            "owner-1",
            "media",
            NOW.replace(tzinfo=None),
            None,
            IMAGE_CONTENT,
        ),
    ],
)
@pytest.mark.asyncio
async def test_find_rejects_malformed_adapter_results(bad_story: object) -> None:
    current = target(1)
    discovery, _, _ = service({current.id: (bad_story,)})  # type: ignore[dict-item]

    with pytest.raises(DiscoveryError, match="^invalid discovery result$"):
        await discovery.find(
            DiscoveryContext(USER_ID, ACCOUNT_ID, NOW),
            (current,),
            ActionBudget(1, 0, 0, 1),
        )


@pytest.mark.parametrize(
    "history",
    [
        object(),
        DiscoveryHistory(frozenset({"bad"}), ()),
        DiscoveryHistory(
            frozenset(),
            (TargetEngagement(uuid4(), NOW - timedelta(days=1)),),
        ),
        DiscoveryHistory(
            frozenset(),
            (TargetEngagement(target(1).id, NOW.replace(tzinfo=None)),),
        ),
    ],
)
@pytest.mark.asyncio
async def test_find_rejects_malformed_history_results(history: object) -> None:
    current = target(1)
    discovery, _, _ = service({current.id: (story(current, 1),)}, history)

    with pytest.raises(DiscoveryError, match="^invalid discovery result$"):
        await discovery.find(
            DiscoveryContext(USER_ID, ACCOUNT_ID, NOW),
            (current,),
            ActionBudget(1, 0, 0, 1),
        )


@pytest.mark.asyncio
async def test_find_redacts_adapter_and_repository_exceptions() -> None:
    current = target(1)
    for adapter_error, repository_error in (
        (RuntimeError(SECRET), None),
        (None, RuntimeError(SECRET)),
    ):
        adapter = FakeAdapter({current.id: (story(current, 1),)}, adapter_error)
        repository = FakeHistoryRepository(error=repository_error)
        discovery = DiscoveryService(adapter, repository)

        with pytest.raises(DiscoveryError, match="^story discovery failed$") as raised:
            await discovery.find(
                DiscoveryContext(USER_ID, ACCOUNT_ID, NOW),
                (current,),
                ActionBudget(1, 0, 0, 1),
            )

        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None
        rendered = "".join(
            traceback.format_exception(
                type(raised.value), raised.value, raised.value.__traceback__
            )
        )
        assert SECRET not in rendered


@pytest.mark.asyncio
async def test_find_propagates_adapter_and_repository_cancellation() -> None:
    current = target(1)
    for adapter_error, repository_error in (
        (asyncio.CancelledError(), None),
        (None, asyncio.CancelledError()),
    ):
        adapter = FakeAdapter({current.id: (story(current, 1),)}, adapter_error)
        repository = FakeHistoryRepository(error=repository_error)
        discovery = DiscoveryService(adapter, repository)

        with pytest.raises(asyncio.CancelledError):
            await discovery.find(
                DiscoveryContext(USER_ID, ACCOUNT_ID, NOW),
                (current,),
                ActionBudget(1, 0, 0, 1),
            )


def test_story_domain_values_are_frozen_and_slotted() -> None:
    current = target(1)
    opportunity = StoryOpportunity(
        target_id=current.id,
        owner_id=current.owner_id,
        target_handle=current.handle,
        tier=current.tier,
        media_id="media",
        taken_at=NOW,
        fingerprint="ab" * 32,
        content=IMAGE_CONTENT,
    )

    with pytest.raises((FrozenInstanceError, AttributeError)):
        opportunity.tier = "discovery"  # type: ignore[misc]
    assert not hasattr(opportunity, "__dict__")


def _production_traceback_text(error: BaseException) -> str:
    captured = traceback.TracebackException(
        type(error),
        error,
        error.__traceback__,
        capture_locals=True,
    )
    return "\n".join(
        f"{frame.name}: {frame.locals!r}"
        for frame in captured.stack
        if "/engagemate/worker/" in frame.filename
    )


@pytest.mark.parametrize(
    "boundary",
    [
        "context",
        "adapter_container",
        "adapter_item",
        "history",
        "history_item",
        "adapter_error",
        "history_error",
    ],
)
@pytest.mark.asyncio
async def test_find_removes_secret_objects_from_error_traceback_locals(
    boundary: str,
) -> None:
    current = target(1)
    context: object = DiscoveryContext(USER_ID, ACCOUNT_ID, NOW)
    adapter: FakeAdapter = FakeAdapter({current.id: (story(current, 1),)})
    repository: FakeHistoryRepository = FakeHistoryRepository()
    if boundary == "context":
        context = SecretObject()
    elif boundary == "adapter_container":
        adapter.stories[current.id] = SecretObject()  # type: ignore[assignment]
    elif boundary == "adapter_item":
        adapter.stories[current.id] = (SecretObject(),)  # type: ignore[assignment]
    elif boundary == "history":
        repository.result = SecretObject()
    elif boundary == "history_item":
        repository.result = DiscoveryHistory(frozenset(), (SecretObject(),))  # type: ignore[arg-type]
    elif boundary == "adapter_error":
        adapter.error = RuntimeError(CUSTOM_SECRET)
    else:
        repository.error = RuntimeError(CUSTOM_SECRET)

    discovery = DiscoveryService(adapter, repository)
    with pytest.raises(DiscoveryError) as raised:
        await discovery.find(
            context,  # type: ignore[arg-type]
            (current,),
            ActionBudget(1, 0, 0, 1),
        )

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert CUSTOM_SECRET not in _production_traceback_text(raised.value)


@pytest.mark.parametrize("boundary", ["context", "story", "history"])
@pytest.mark.asyncio
async def test_find_rejects_datetime_subclasses_without_retaining_their_repr(
    boundary: str,
) -> None:
    current = target(1)
    evil = EvilDate(2026, 7, 31, 8, tzinfo=UTC)
    context = DiscoveryContext(USER_ID, ACCOUNT_ID, NOW)
    adapter = FakeAdapter({current.id: (story(current, 1),)})
    repository = FakeHistoryRepository()
    if boundary == "context":
        context = DiscoveryContext(USER_ID, ACCOUNT_ID, evil)
    elif boundary == "story":
        adapter.stories[current.id] = (replace(story(current, 1), taken_at=evil),)
    else:
        repository.result = DiscoveryHistory(
            frozenset(),
            (TargetEngagement(current.id, evil),),
        )

    discovery = DiscoveryService(adapter, repository)
    with pytest.raises(DiscoveryError) as raised:
        await discovery.find(
            context,
            (current,),
            ActionBudget(1, 0, 0, 1),
        )

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert CUSTOM_SECRET not in _production_traceback_text(raised.value)


@pytest.mark.asyncio
async def test_find_rejects_duplicate_owner_or_handle_identity() -> None:
    first = target(1)
    duplicate_owner = replace(target(2), owner_id=first.owner_id)
    duplicate_handle = replace(target(3), handle=first.handle)
    discovery, adapter, repository = service({})

    for duplicate in (duplicate_owner, duplicate_handle):
        with pytest.raises(DiscoveryError, match="^invalid discovery input$"):
            await discovery.find(
                DiscoveryContext(USER_ID, ACCOUNT_ID, NOW),
                (first, duplicate),
                ActionBudget(1, 0, 0, 1),
            )
    assert adapter.calls == []
    assert repository.calls == []


@pytest.mark.asyncio
async def test_find_rejects_seen_fingerprints_outside_queried_batch() -> None:
    current = target(1)
    discovery, _, _ = service(
        {current.id: (story(current, 1),)},
        DiscoveryHistory(frozenset({"ab" * 32}), ()),
    )

    with pytest.raises(DiscoveryError, match="^invalid discovery result$"):
        await discovery.find(
            DiscoveryContext(USER_ID, ACCOUNT_ID, NOW),
            (current,),
            ActionBudget(1, 0, 0, 1),
        )


@pytest.mark.asyncio
async def test_find_carries_handle_and_immutable_image_content_to_task_10() -> None:
    current = target(1)
    active = story(current, 1)
    discovery, _, _ = service({current.id: (active,)})

    result = await discovery.find(
        DiscoveryContext(USER_ID, ACCOUNT_ID, NOW),
        (current,),
        ActionBudget(1, 0, 0, 1),
    )

    assert result[0].target_handle == current.handle
    assert result[0].content is IMAGE_CONTENT
    assert result[0].content.image is JPEG
    assert "story-image" not in repr(result[0].content)
    assert "story-image" not in repr(result[0].content.image)


@pytest.mark.asyncio
async def test_find_carries_one_to_four_immutable_video_frames() -> None:
    current = target(1)
    frames = tuple(
        StoryMedia("image/jpeg", f"frame-{index}".encode()) for index in range(1, 5)
    )
    content = StoryContent(
        media_type=StoryMediaType.VIDEO,
        caption_text=None,
        sticker_text="Question",
        image=None,
        video_frames=frames,
    )
    active = replace(story(current, 1), content=content)
    discovery, _, _ = service({current.id: (active,)})

    result = await discovery.find(
        DiscoveryContext(USER_ID, ACCOUNT_ID, NOW),
        (current,),
        ActionBudget(1, 0, 0, 1),
    )

    assert result[0].content.video_frames == frames
    assert not hasattr(result[0].content, "__dict__")
    assert not hasattr(frames[0], "__dict__")


@pytest.mark.parametrize(
    "content",
    [
        SecretObject(),
        StoryContent(StoryMediaType.IMAGE, None, None, None, ()),
        StoryContent(
            StoryMediaType.IMAGE,
            None,
            None,
            StoryMedia("text/plain", b"image"),
            (),
        ),
        StoryContent(
            StoryMediaType.IMAGE,
            "x" * 4001,
            None,
            JPEG,
            (),
        ),
        StoryContent(
            StoryMediaType.IMAGE,
            None,
            None,
            StoryMedia("image/jpeg", b"x" * (5 * 1024 * 1024 + 1)),
            (),
        ),
        StoryContent(
            StoryMediaType.VIDEO,
            None,
            None,
            None,
            (),
        ),
        StoryContent(
            StoryMediaType.VIDEO,
            None,
            None,
            None,
            (JPEG, JPEG, JPEG, JPEG, JPEG),
        ),
        StoryContent(
            StoryMediaType.VIDEO,
            None,
            None,
            JPEG,
            (JPEG,),
        ),
        StoryContent(
            StoryMediaType.VIDEO,
            None,
            None,
            None,
            (StoryMedia("image/jpeg", bytearray(b"mutable")),),  # type: ignore[arg-type]
        ),
    ],
)
@pytest.mark.asyncio
async def test_find_rejects_malformed_or_oversize_content_without_leaking_bytes(
    content: object,
) -> None:
    current = target(1)
    active = replace(story(current, 1), content=content)
    discovery, _, _ = service({current.id: (active,)})

    with pytest.raises(DiscoveryError, match="^invalid discovery result$") as raised:
        await discovery.find(
            DiscoveryContext(USER_ID, ACCOUNT_ID, NOW),
            (current,),
            ActionBudget(1, 0, 0, 1),
        )

    assert CUSTOM_SECRET not in _production_traceback_text(raised.value)
    assert "mutable" not in _production_traceback_text(raised.value)


@pytest.mark.asyncio
async def test_content_does_not_change_media_id_fingerprint() -> None:
    current = target(1)
    first = story(current, 1)
    different_content = StoryContent(
        StoryMediaType.IMAGE,
        "Different caption",
        None,
        StoryMedia("image/png", b"different-image"),
        (),
    )
    second = replace(first, content=different_content)
    selected: list[StoryOpportunity] = []
    for stories in ((first, second), (second, first)):
        discovery, _, _ = service({current.id: stories})
        result = await discovery.find(
            DiscoveryContext(USER_ID, ACCOUNT_ID, NOW),
            (current,),
            ActionBudget(2, 0, 0, 2),
        )
        assert len(result) == 1
        selected.extend(result)

    assert selected[0].fingerprint == selected[1].fingerprint
    assert selected[0].content == selected[1].content


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["authentication_required", "checkpoint_required", "rate_limited", "story_unavailable"])
async def test_safe_preview_reasons_survive_discovery_without_private_diagnostics(reason):
    error = RuntimeError(SECRET)
    error.reason = reason
    current = target(1)
    repository = FakeHistoryRepository()
    discovery = DiscoveryService(FakeAdapter({}, error), repository)
    with pytest.raises(DiscoveryError, match="^story discovery failed$") as raised:
        await discovery.find(
            DiscoveryContext(USER_ID, ACCOUNT_ID, NOW), (current,),
            ActionBudget(likes=1, replies=0, dms=0, plan_total=1),
        )
    assert raised.value.reason == reason
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert SECRET not in _production_traceback_text(raised.value)
    assert repository.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [SECRET, None, {"private": SECRET}, 123])
async def test_arbitrary_dependency_reasons_remain_redacted(reason):
    error = RuntimeError(SECRET)
    error.reason = reason
    current = target(1)
    discovery = DiscoveryService(FakeAdapter({}, error), FakeHistoryRepository())
    with pytest.raises(DiscoveryError, match="^story discovery failed$") as raised:
        await discovery.find(
            DiscoveryContext(USER_ID, ACCOUNT_ID, NOW), (current,),
            ActionBudget(likes=1, replies=0, dms=0, plan_total=1),
        )
    assert raised.value.reason == "story_unreadable"
    assert SECRET not in _production_traceback_text(raised.value)
