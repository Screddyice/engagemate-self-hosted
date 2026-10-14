from datetime import UTC, datetime, timedelta

import pytest

from engagemate.worker.follow_reels import Reel, choose_reel, compose_message


def test_rotates_three_newest_owned_reels_ignoring_pins_and_duplicates():
    now = datetime.now(UTC)
    reels = [Reel(f'reel_{i}', now-timedelta(days=i), '123') for i in range(5)]
    reels += [reels[0], Reel('alien_1', now, '456'), Reel('future1', now+timedelta(days=1), '123')]
    assert [choose_reel(list(reversed(reels)), '123', i).code for i in range(7)] == [
        'reel_0', 'reel_1', 'reel_2', 'reel_0', 'reel_1', 'reel_2', 'reel_0']


def test_requires_three_verified_reels():
    with pytest.raises(ValueError, match='three verified sender reels'):
        choose_reel([], '123', 0)


@pytest.mark.parametrize('url', ['http://www.instagram.com/reel/abcde/',
    'https://evil.example/reel/abcde/', 'https://www.instagram.com/reel/abcde/?next=x',
    'https://user@www.instagram.com/reel/abcde/', 'https://www.instagram.com:443/reel/abcde/'])
def test_rejects_noncanonical_reel_url(url):
    with pytest.raises(ValueError):
        compose_message(url)


@pytest.mark.parametrize('template', ['', ' '*3, 'x'*501, 'hello\nthere', 'hi\x7f',
    'see https://example.com', 'www.example.com', 'example.com', 'ftp://example', None])
def test_rejects_unsafe_message_templates(template):
    with pytest.raises(ValueError):
        compose_message('https://www.instagram.com/reel/abcde/', template)


def test_message_uses_generic_default_or_user_template():
    url = 'https://www.instagram.com/reel/abcde/'
    assert 'Sam' not in compose_message(url)
    assert compose_message(url, '  Thanks! -Sam  ') == f'Thanks! -Sam\n{url}'
