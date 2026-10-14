import pytest
from engagemate.worker.follow_reel_reads import reel_candidates, verified_reel_detail


def candidate_payload(owner='123'):
    return {'data': {'fetch__XDTUserDict': {'clips_connection': {
        'edges': [{'node': {'media': {'code': code, 'pk': str(i+10),
             'product_type': 'clips', 'user': {'pk': owner},
             'clips_tab_pinned_user_ids': [owner] if i == 0 else []}}}
            for i, code in enumerate(['oldpin', 'newest', 'middle', 'oldest'])],
        'page_info': {'has_next_page': False}}}}}



def test_pins_and_reordered_items_are_retained_for_publication_sorting():
    candidates,cursor = reel_candidates(candidate_payload(),'123')
    assert len(candidates)==4 and candidates[0]['pinned'] and cursor is None


@pytest.mark.parametrize('field,value',[('clips_tab_pinned_user_ids',None),('code','bad/url')])
def test_invalid_grid_nodes_fail_closed(field,value):
    p=candidate_payload()
    p['data']['fetch__XDTUserDict']['clips_connection']['edges'][1]['node']['media'][field]=value
    with pytest.raises(ValueError): reel_candidates(p,'123')


@pytest.mark.parametrize('field,value',[('product_type','feed'),('user',{'pk':'456'})])
def test_non_reels_and_foreign_owners_are_excluded(field,value):
    p=candidate_payload()
    p['data']['fetch__XDTUserDict']['clips_connection']['edges'][1]['node']['media'][field]=value
    assert len(reel_candidates(p,'123')[0]) == 3


@pytest.mark.parametrize('cursor',[None,'',True,'bad\ncursor'])
def test_pagination_requires_valid_native_cursor(cursor):
    p=candidate_payload()
    p['data']['fetch__XDTUserDict']['clips_connection']['page_info']={'has_next_page':True,'end_cursor':cursor}
    with pytest.raises(ValueError): reel_candidates(p,'123')


@pytest.mark.parametrize('field,value',[('pk','99'),('code','other1'),('taken_at',None),('taken_at',True),('taken_at',999999999999),('user',{'pk':'456'}),('product_type','feed')])
def test_detail_is_bound_to_exact_reel(field,value):
    media={'pk':'10','code':'abcde','taken_at':1700000000,'product_type':'clips','user':{'pk':'123'}}
    media[field]=value
    with pytest.raises(ValueError): verified_reel_detail({'status':'ok','items':[media]},{'id':'10','code':'abcde'},'123')


def test_detail_uses_authoritative_publication_date():
    media={'pk':'10','code':'abcde','taken_at':1700000000,'product_type':'clips','user':{'pk':'123'}}
    reel=verified_reel_detail({'status':'ok','items':[media]},{'id':'10','code':'abcde'},'123')
    assert reel.posted_at.timestamp()==1700000000
