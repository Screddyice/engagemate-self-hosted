from datetime import UTC, datetime, timedelta

import httpx
import pytest

from engagemate.reports.canary import run
from engagemate.reports.composio import ComposioEmailClient
from engagemate.reports.periodic import display_delta
from tests.worker.test_canary_scripts import database_url, world  # noqa: F401

NOW=datetime(2026,9,15,16,tzinfo=UTC)


def args(world):
    return dict(user_id=world['user_id'],account_id=world['account_id'],kind='fortnightly',now=NOW)


def test_canary_preview_is_read_only_and_unknown_is_not_zero(world):
    result=run(world['pool'],None,**args(world))
    assert result['status']=='preview'
    assert 'TEST EMAIL' in result['text'] and 'partial-period test' in result['text']
    assert 'no verified follow-back observations' in result['text']
    assert 'Incoming responses: not available' in result['text']
    with world['pool'].connection() as c:
        assert c.execute('SELECT count(*) n FROM periodic_report_deliveries').fetchone()['n']==0
        assert c.execute('SELECT count(*) n FROM periodic_report_preferences').fetchone()['n']==0


def test_canary_rejects_other_tenant_account(world):
    values=args(world);values['user_id']=world['other_user_id']
    with pytest.raises(ValueError,match='owned account'):
        run(world['pool'],None,**values)


def test_live_canary_reexecution_uses_existing_delivery_even_if_metrics_change(world):
    calls=[]
    def handle(request):
        calls.append(request)
        return httpx.Response(200,json={'successful':True,'data':{'response_data':{'id':'canary-1'}}})
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client=ComposioEmailClient(pool=world['pool'],http=http,api_key='test',account_id='test',sender='test@example.test')
        first=run(world['pool'],client,send=True,**args(world))
        assert first=={'status':'sent','provider_id':'canary-1'}
        values=args(world);values['now']+=timedelta(minutes=5)
        second=run(world['pool'],client,send=True,**values)
        assert second=={'status':'already_sent','provider_id':'canary-1'}
        assert len(calls)==1


def test_current_observation_is_visible_when_starting_baseline_is_missing():
    assert 'Latest observed: 1,234' in display_delta(None,1234)
    assert 'Latest observed: 2.34%' in display_delta(None,234,rate=True)


def test_canary_revision_allows_new_content_but_dedupes_that_revision(world):
    calls=[]
    def handle(request):
        calls.append(request)
        return httpx.Response(200,json={'successful':True,'data':{'id':f'canary-{len(calls)}'}})
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client=ComposioEmailClient(pool=world['pool'],http=http,api_key='test',account_id='test',sender='test@example.test')
        assert run(world['pool'],client,send=True,**args(world))['provider_id']=='canary-1'
        revised=run(world['pool'],client,send=True,revision='blue-v1',**args(world))
        assert revised=={'status':'sent','provider_id':'canary-2'}
        assert run(world['pool'],client,send=True,revision='blue-v1',**args(world))=={'status':'already_sent','provider_id':'canary-2'}
    assert len(calls)==2


@pytest.mark.parametrize('revision',['', 'a'*33, '../new', 'has space'])
def test_canary_revision_rejects_invalid_labels(world,revision):
    with pytest.raises(ValueError,match='invalid canary revision'):
        run(world['pool'],None,revision=revision,**args(world))
