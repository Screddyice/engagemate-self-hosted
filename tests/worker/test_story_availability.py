from engagemate.worker.story_availability import _UNAVAILABLE_STATUS, loaded_story_media, story_media_unavailable
import pytest


@pytest.mark.parametrize("text", [
    "This story is unavailable", "This story is no longer available.",
    "Story unavailable", "This story has expired", "This story has been deleted",
    "Sorry, this story is no longer available.", "This post is unavailable.",
    "This content isn't available right now", "This content isn’t available right now.",
])
def test_explicit_status_phrases(text):
    assert _UNAVAILABLE_STATUS.fullmatch(text)


@pytest.mark.parametrize("text", [
    "I'm unavailable tomorrow", "Tickets unavailable", "Unavailable",
    "My story has expired coupons", "I can't believe this story is unavailable",
    "This content isn't available right now because I'm editing it",
])
def test_caption_keywords_are_not_native_status(text):
    assert not _UNAVAILABLE_STATUS.fullmatch(text)


@pytest.mark.parametrize("source,tag,allowed", [
    ("https://scontent.cdninstagram.com/story.jpg?x=1", "IMG", True),
    ("https://video.fbcdn.net/story.mp4", "VIDEO", True),
    ("blob:https://www.instagram.com/123", "VIDEO", True),
    ("blob:https://www.instagram.com/123", "IMG", False),
    ("https://static.cdninstagram.com/story_unavailable.png", "IMG", False),
    ("https://www.instagram.com/static/error.png", "IMG", False),
    ("https://cdninstagram.com.attacker.test/story.jpg", "IMG", False),
    ("data:image/png;base64,AAAA", "IMG", False),
    ("", "IMG", False),
])
def test_only_actual_media_sources_pass(source, tag, allowed):
    assert loaded_story_media({"tag": tag, "source": source, "loaded": True, "width": 720, "height": 1280}) is allowed


@pytest.mark.parametrize("change", [{"loaded": False}, {"width": 32}, {"height": 0}, {"width": True}])
def test_loading_and_intrinsic_dimensions_required(change):
    evidence = {"tag": "IMG", "source": "https://scontent.cdninstagram.com/story.jpg", "loaded": True, "width": 720, "height": 1280}
    evidence.update(change)
    assert not loaded_story_media(evidence)


@pytest.mark.parametrize("name", ["story_unavailable.png", "placeholder.jpg"])
def test_known_error_asset_is_unavailable_even_before_it_loads(name):
    assert story_media_unavailable({
        "source": "https://static.cdninstagram.com/" + name, "loaded": False,
    })
