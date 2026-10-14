import pytest
from engagemate.growth.owned import owned_profile, owned_posts, parse_growth_query
from engagemate.growth.metrics import parse_profile_metrics
from engagemate.worker.follower_browser import FollowerBrowserError


def profile(**changes):
    row=dict(pk='123',id='123',username='owner',follower_count=100,following_count=20,media_count=5,friendship_status=None)
    row.update(changes)
    return {'data':{'user':row}}


def test_own_profile_does_not_require_a_friendship_with_itself():
    assert owned_profile(profile(),'owner','123')=={'followersCount':100,'followingCount':20,'postsCount':5}


@pytest.mark.parametrize('changes',[{'id':'999'},{'username':'other'},{'follower_count':True},{'follower_count':-1}])
def test_owner_identity_and_counts_are_strict(changes):
    with pytest.raises(FollowerBrowserError):
        owned_profile(profile(**changes),'owner','123')


def test_conflicting_owned_counts_fail_closed():
    with pytest.raises(FollowerBrowserError):
        owned_profile({'rows':[profile(),profile(follower_count=200)]},'owner','123')


def test_posts_are_owner_scoped_deduplicated_and_hidden_counts_excluded():
    rows=[]
    for i in range(15):
        rows.append(dict(pk=str(i+1),user={'pk':'123','id':'123'},media_type=1,like_count=i,comment_count=2,taken_at=1700000000+i))
    rows.extend([dict(rows[0]),dict(rows[1],pk='99',like_count=-1),dict(rows[2],pk='100',user={'pk':'other'})])
    posts=owned_posts({'rows':rows},'123')
    assert len(posts)==12
    assert posts[0]['likesCount']==14 and posts[-1]['likesCount']==3


def test_failed_optional_posts_preserve_verified_follower_snapshot():
    own = parse_growth_query('PolarisProfilePageContentQuery', 200, profile(), 'owner', '123')
    posts = parse_growth_query('PolarisProfilePostsQuery', 200, {'status':'fail'}, 'owner', '123')
    assert posts is None
    assert parse_profile_metrics(own).status == 'partial'
    assert parse_profile_metrics(own).follower_count == 100


@pytest.mark.parametrize('status,reason',[(401,'authentication_required'),(429,'rate_limited')])
def test_posts_security_failures_still_block_snapshot(status, reason):
    with pytest.raises(FollowerBrowserError) as exc:
        parse_growth_query('PolarisProfilePostsQuery',status,None,'owner','123')
    assert exc.value.reason == reason


def test_posts_server_error_is_optional_but_profile_server_error_is_not():
    assert parse_growth_query('PolarisProfilePostsQuery',500,None,'owner','123') is None
    with pytest.raises(FollowerBrowserError):
        parse_growth_query('PolarisProfilePageContentQuery',500,None,'owner','123')


@pytest.mark.parametrize('payload', [None, {'message': 'csrf_failed'}, profile()])
def test_forbidden_optional_posts_preserve_snapshot_but_never_verify_profile(payload):
    assert parse_growth_query('PolarisProfilePostsQuery',403,payload,'owner','123') is None
    with pytest.raises(FollowerBrowserError, match='^response_failed$'):
        parse_growth_query('PolarisProfilePageContentQuery',403,payload,'owner','123')


@pytest.mark.parametrize('payload,reason', [
    ({'message': 'login_required'}, 'authentication_required'),
    ({'require_login': True}, 'authentication_required'),
    ({'challenge': {'url': 'hidden'}}, 'checkpoint_required'),
    ({'message': 'Please wait a few minutes'}, 'rate_limited'),
])
def test_forbidden_optional_posts_explicit_restrictions_still_block_snapshot(payload, reason):
    with pytest.raises(FollowerBrowserError, match='^' + reason + '$'):
        parse_growth_query('PolarisProfilePostsQuery',403,payload,'owner','123')
