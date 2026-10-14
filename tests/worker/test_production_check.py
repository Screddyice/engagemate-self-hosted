"""Scoped production operator checks retain saved guards and ordinary canaries."""

from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import timedelta
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest

from engagemate.domain.budgets import ActionBudget
from engagemate.domain.models import Action, JobStatus
from engagemate.worker import canary, production_check
from engagemate.worker.canary import CanaryError, CanaryScope, FreshExactJobRepository, ProductionReplyContextLoader
from engagemate.worker.follower_store import PostgresFollowerStore
from engagemate.worker.follow_store import FollowStoreScope
from engagemate.worker.story_types import DiscoveryContext
from tests.worker.test_runner import FakeLoader, claimed_job, run_context
from tests.worker.test_canary_scripts import database_url, world  # noqa: F401
from tests.worker.test_follow_store import NOW, ready, sql  # noqa: F401
from tests.worker.test_scoped_follow_reel_stores import scoped_ready  # noqa: F401


class BorrowedPool:
    def __init__(self,pool):
        self.connection=pool.connection
    def close(self):
        pass


@pytest.mark.parametrize('case',['fresh','attempted','running','foreign','paused'])
async def test_discard_only_cancels_exact_fresh_owned_job(world,monkeypatch,case):  # noqa: F811
    from engagemate import config
    from engagemate.db import pool
    scope=CanaryScope(world['other_user_id'] if case=='foreign' else world['user_id'],
                     world['account_id'],world['job_id'],'discard')
    sql(world,"UPDATE jobs SET status=%s,attempts=%s WHERE id=%s",
        ('running' if case=='running' else 'queued',1 if case=='attempted' else 0,scope.job_id))
    if case=='paused':
        sql(world,"UPDATE ig_accounts SET paused_at=now() WHERE id=%s",(scope.account_id,))
    before=sql(world,'SELECT * FROM jobs ORDER BY id')
    monkeypatch.setattr(config,'Settings',lambda:NS(database_url='fixture'))
    monkeypatch.setattr(pool,'create_pool',lambda *a,**k:BorrowedPool(world['pool']))
    if case=='fresh':
        assert await production_check.discard_job(scope)=={'job_id':str(scope.job_id),'status':'cancelled'}
        assert sql(world,'SELECT status,attempts,last_error FROM jobs WHERE id=%s',(scope.job_id,))==[
            ('cancelled',1,'operator_check_not_started')]
    else:
        with pytest.raises(CanaryError,match='fresh_job_unavailable_for_discard'):
            await production_check.discard_job(scope)
        assert sql(world,'SELECT * FROM jobs ORDER BY id')==before


@pytest.mark.parametrize("sources,replies,expected", [([],2,"approved_follower_sources_required"),
    ([{}],0,"budget_exhausted"),([{}],2,None)])
async def test_reply_context_requires_source_and_saved_budget(sources, replies, expected):
    job = claimed_job()
    context = replace(run_context(), budget=ActionBudget(3,replies,0,10,views=20),
                      config_snapshot={"follower_sources": sources})
    scope = CanaryScope(job.user_id,job.ig_account_id,job.id,job.claimed_by,reply_only=True)
    loader = ProductionReplyContextLoader(FakeLoader(context),scope)
    if expected:
        with pytest.raises(canary.RunContextError,match=expected):
            await loader.load(job)
    else:
        result = await loader.load(job)
        assert result.budget.replies == result.budget.private_messages == 1
        assert result.budget.likes == result.budget.views == result.budget.dms == 0
        assert result.config_snapshot["production_check"]["execution_mode"] == "autonomous"
        assert "canary" not in result.config_snapshot and context.budget.replies == 2


async def test_fresh_job_port_claims_only_queued_exact_job(world):  # noqa: F811
    scope = CanaryScope(world['user_id'],world['account_id'],world['job_id'],'production-check',True)
    sql(world,"UPDATE jobs SET status='queued',attempts=0,claimed_by=NULL,lease_expires_at=NULL WHERE id=%s",(scope.job_id,))
    port = FreshExactJobRepository(world['pool'],scope,lease_seconds=120)
    job = await port.claim_next(scope.worker_id)
    assert job.id == scope.job_id and job.attempts == 1
    assert await port.claim_next(scope.worker_id) is None


async def test_scoped_canary_defer_finishes_the_exact_job(world):  # noqa: F811
    scope = CanaryScope(
        world['user_id'], world['account_id'], world['job_id'], 'production-check', True
    )
    sql(world, "UPDATE jobs SET status='queued',attempts=0,claimed_by=NULL,lease_expires_at=NULL WHERE id=%s", (scope.job_id,))
    port = FreshExactJobRepository(world['pool'], scope, lease_seconds=120)
    job = await port.claim_next(scope.worker_id)

    assert await port.defer(job.id, scope.worker_id, 'proxy_transient') is True
    assert sql(world, 'SELECT status,attempts,last_error FROM jobs WHERE id=%s', (scope.job_id,)) == [
        ('failed', 1, 'proxy_transient')
    ]


@pytest.mark.parametrize("mode",['disabled','canary'])
@pytest.mark.parametrize("lane",['reply','view','like','follow-reel'])
async def test_operator_rejects_saved_nonautonomous_mode_before_pool(monkeypatch,mode,lane):
    from deploy import healthcheck
    from engagemate import config
    from engagemate.db import pool
    monkeypatch.setattr(healthcheck,'validate_worker_environment',lambda _:None)
    monkeypatch.setattr(config,'Settings',lambda:NS(instagram_execution_mode=mode))
    monkeypatch.setattr(pool,'create_pool',lambda *a,**k:pytest.fail('pool touched'))
    with pytest.raises(CanaryError,match='saved_autonomous'):
        if lane == 'reply':
            await production_check.run_reply(CanaryScope(uuid4(),uuid4(),uuid4(),'check',True))
        elif lane in ('view','like'):
            await production_check.run_story(CanaryScope(uuid4(),uuid4(),uuid4(),'check'),Action(lane))
        else:
            await production_check.run_follow_reel(FollowStoreScope(uuid4(),uuid4(),uuid4()),worker_id='check')


def source_for(w):
    profile,source = uuid4(),uuid4()
    sql(w,"""INSERT INTO brand_profiles
        (id,user_id,ig_account_id,reference_url,reference_username,follower_audience_approved_at)
        VALUES (%s,%s,%s,'https://www.instagram.com/source/','source',%s)""",
        (profile,w['user_id'],w['account_id'],NOW))
    sql(w,"""INSERT INTO brand_reference_accounts (id,brand_profile_id,username,profile_url,position)
        VALUES (%s,%s,'source','https://www.instagram.com/source/',0)""",(source,profile))
    return source


@pytest.mark.parametrize('prior',['none','sent','failed','uncertain','relationship'])
async def test_new_prospect_store_refuses_prior_outcomes(scoped_ready,prior):
    w=scoped_ready
    context=DiscoveryContext(w['user_id'],w['account_id'],NOW)
    source=source_for(w)
    ordinary=PostgresFollowerStore(w['pool'])
    store=PostgresFollowerStore(w['pool'],require_new_relationship=True)
    await ordinary.save_page(context,source,None,None,True,[{'owner_id':'123456','handle':'target.one'}])
    if prior=='none':
        sql(w,'DELETE FROM hermes_tool_calls WHERE run_id=%s',(w['run_id'],))
        sql(w,'DELETE FROM story_engagements WHERE id=%s',(w['engagement_id'],))
    elif prior=='failed':
        sql(w,"UPDATE story_engagements SET status='failed' WHERE id=%s",(w['engagement_id'],))
    elif prior=='uncertain':
        sql(w,'DELETE FROM story_engagements WHERE id=%s',(w['engagement_id'],))
        sql(w,"UPDATE hermes_tool_calls SET status='uncertain' WHERE run_id=%s",(w['run_id'],))
    elif prior=='relationship':
        await w['store'].enqueue_confirmed(NOW)
    assert await ordinary.next_candidate(context,[source],NOW) is not None
    assert (await store.next_candidate(context,[source],NOW) is not None)==(prior=='none')


@pytest.mark.parametrize('value',[None,1,'true'])
def test_new_prospect_selection_requires_boolean(value):
    with pytest.raises(ValueError):
        PostgresFollowerStore(None,require_new_relationship=value)


@pytest.mark.parametrize('outcome',['confirmed','uncertain','deferred'])
async def test_follow_reel_operator_uses_only_exact_stores(scoped_ready,monkeypatch,outcome):
    from deploy import healthcheck
    from engagemate import config
    from engagemate.db import pool
    from engagemate.worker import follow_runtime,follow_reel_runtime
    from playwright import async_api
    w=scoped_ready
    scope=FollowStoreScope(w['user_id'],w['account_id'],w['engagement_id'])
    monkeypatch.setattr(healthcheck,'validate_worker_environment',lambda _:None)
    monkeypatch.setattr(config,'Settings',lambda:NS(instagram_execution_mode='autonomous',database_url='fixture'))
    monkeypatch.setattr(pool,'create_pool',lambda *a,**k:BorrowedPool(w['pool']))
    events=[]

    @asynccontextmanager
    async def playwright():
        yield object()

    async def follow(claim,before):
        assert claim.user_id==scope.user_id and claim.ig_account_id==scope.account_id
        events.append('follow')
        if outcome!='deferred':
            assert await before()
        return outcome,{'identity_matches':True}

    async def reel(claim,prepare,before):
        events.append('reel')
        url='https://www.instagram.com/reel/newcheck/'
        assert await prepare(url,'Hello\n'+url) and await before()
        return 'sent',{'server_confirmed':True,'sender_id':claim.sender_id,
                       'recipient_id':claim.recipient_id,'thread_id':'111','item_id':'222'}

    monkeypatch.setattr(async_api,'async_playwright',playwright)
    monkeypatch.setattr(follow_runtime,'LiveFollowPerformer',lambda *a:follow)
    monkeypatch.setattr(follow_reel_runtime,'LiveFollowReelPerformer',lambda *a:reel)
    result=await production_check.run_follow_reel(scope,worker_id='check')
    assert events==(['follow','reel'] if outcome=='confirmed' else ['follow'])
    assert (result['reel'] is not None)==(outcome=='confirmed')
    with pytest.raises(CanaryError,match='fresh_confirmed_new_reply'):
        await production_check.run_follow_reel(scope,worker_id='check-again')


@pytest.mark.parametrize('change', ['foreign','like','unconfirmed','old','run_failed'])
def test_follow_reel_prerequisite_requires_exact_recent_confirmed_reply(scoped_ready,change):
    w=scoped_ready
    scope=FollowStoreScope(w['user_id'],w['account_id'],w['engagement_id'])
    if change=='foreign':
        scope=FollowStoreScope(w['other_user_id'],w['account_id'],w['engagement_id'])
    elif change=='like':
        sql(w,"UPDATE story_engagements SET action='like' WHERE id=%s",(w['engagement_id'],))
    elif change=='unconfirmed':
        sql(w,"UPDATE hermes_tool_calls SET status='uncertain' WHERE run_id=%s",(w['run_id'],))
    elif change=='old':
        sql(w,'UPDATE story_engagements SET executed_at=%s WHERE id=%s',(NOW-timedelta(minutes=31),w['engagement_id']))
    else:
        sql(w,"UPDATE hermes_execution_runs SET status='failed' WHERE id=%s",(w['run_id'],))
    with pytest.raises(CanaryError,match='fresh_confirmed_new_reply'):
        production_check._require_new_reply(w['pool'],scope,NOW)


@pytest.mark.parametrize('collision',[False,True])
@pytest.mark.parametrize('action',[None,Action.VIEW,Action.LIKE])
async def test_check_wires_saved_hermes_and_binds_before_fresh_exact_claim(world,monkeypatch,collision,action):  # noqa: F811
    import uvicorn
    from playwright import async_api
    from pydantic import SecretStr
    from deploy import healthcheck
    from engagemate import config
    from engagemate.db import pool
    from engagemate.worker import wiring
    from engagemate.worker.execution_mcp_repository import ExecutionMcpRepository
    from engagemate.worker.hermes_run import HermesRunClient
    from tests.worker.test_canary_entrypoint import Server
    scope=CanaryScope(world['user_id'],world['account_id'],world['job_id'],'check',action is None)
    sql(world,"UPDATE jobs SET status='queued',attempts=0,claimed_by=NULL,lease_expires_at=NULL WHERE id=%s",(scope.job_id,))
    other=uuid4()
    sql(world,"INSERT INTO jobs (id,user_id,ig_account_id,schedule_key,scheduled_for) VALUES (%s,%s,%s,%s,now())",
        (other,scope.user_id,scope.account_id,str(other)))
    events=[]
    identity={}
    with canary._bind_mcp_socket('127.0.0.1',0) as held:
        settings=NS(instagram_execution_mode='autonomous',execution_mcp_host='127.0.0.1',
            execution_mcp_port=held.getsockname()[1] if collision else 0,
            execution_mcp_key=SecretStr('T9x_2AbCdEfGhJkLmNpQrStUvWxYz0123456789-'),
            database_url='fixture',job_lease_seconds=120,hermes_execution_timeout_seconds=300,
            hermes_agent_url='http://127.0.0.1:8642/v1/chat/completions',
            hermes_agent_api_key=SecretStr('test-only-key-for-hermes-client-12345'),hermes_agent_model='engagemate-agent')
        monkeypatch.setattr(config,'Settings',lambda:settings)
        monkeypatch.setattr(healthcheck,'validate_worker_environment',lambda _:None)
        def create(*a,**k):
            events.append('pool')
            return BorrowedPool(world['pool'])
        monkeypatch.setattr(pool,'create_pool',create)
        @asynccontextmanager
        async def playwright():
            yield object()
        monkeypatch.setattr(async_api,'async_playwright',playwright)
        monkeypatch.setattr(uvicorn,'Server',lambda _:Server(events))
        async def forbidden(*a):
            pytest.fail('global recovery invoked')
        monkeypatch.setattr(ExecutionMcpRepository,'reconcile_abandoned_runs',forbidden)
        def build(*a,**kw):
            assert kw['execution_mode']=='autonomous' and isinstance(kw['hermes'],HermesRunClient)
            assert isinstance(kw['jobs'],FreshExactJobRepository)
            assert kw['follower_store']._require_new_relationship is True
            async def run(worker):
                assert events==['pool','mcp_ready']
                assert await kw['tool_repository'].reconcile_abandoned_runs()==0
                job=await kw['jobs'].claim_next(worker)
                context=replace(run_context(),user_id=scope.user_id,ig_account_id=scope.account_id,
                                config_snapshot={'follower_sources':[{}]})
                loaded=await kw['context_loader_factory'](FakeLoader(context)).load(job)
                assert loaded.budget.engagements==int(action is not Action.VIEW)
                assert loaded.budget.views==int(action is Action.VIEW)
                assert 'production_check' in loaded.config_snapshot
                if action is not None:
                    # Prove only the first exact Story attempt reaches the base
                    # repository, even if a no-write skip restored allowance.
                    calls=[]
                    async def begin(self,**values):
                        calls.append(values)
                        return {'allowed':True,'replay':False}
                    monkeypatch.setattr(ExecutionMcpRepository,'begin_mutation',begin)
                    values={'run_id':uuid4(),'job_id':job.id,'user_id':scope.user_id,
                            'ig_account_id':scope.account_id,'worker_id':worker,
                            'opportunity_id':uuid4(),'target_id':uuid4(),
                            'story_fingerprint':'a'*64,'idempotency_key':'check-action-0001','action':action.value}
                    repo=kw['tool_repository']
                    from engagemate.worker.story_fingerprint import fingerprint_story
                    from engagemate.worker.story_types import StoryOpportunity
                    values['story_fingerprint']=fingerprint_story('123456','987654_123456',NOW,None)
                    measured=StoryOpportunity(values['target_id'],'123456','target','vip','987654_123456',
                                              NOW,values['story_fingerprint'],NS(private='do not report'))
                    active=NS(run_id=values['run_id'],job=job,context=context,
                              opportunities={values['opportunity_id']:measured})
                    with kw['registry'].activate(active):
                        assert (await repo.begin_mutation(**values))['allowed']
                        assert (await repo.begin_mutation(**values))['allowed']
                    assert identity=={'owner_id':'123456','media_id':'987654_123456',
                                      **{key:values[key] for key in ('target_id','opportunity_id','story_fingerprint')}}
                    for key,changed in [('opportunity_id',uuid4()),('target_id',uuid4()),
                                        ('story_fingerprint','b'*64),('idempotency_key','check-action-0002'),
                                        ('action','reply')]:
                        assert await repo.begin_mutation(**{**values,key:changed})=={
                            'allowed':False,'replay':False,'reason':'production_check_attempt_exhausted'}
                    assert calls==[values,values]
                assert await kw['jobs'].finish(job.id,worker,JobStatus.COMPLETED)
                return NS(status=JobStatus.COMPLETED,engaged=1)
            return NS(run_once=run)
        monkeypatch.setattr(wiring,'build_runner',build)
        if collision:
            with pytest.raises(CanaryError,match='mcp_port_unavailable'):
                await canary._run_scoped_job(scope,production_reply_check=action is None,
                                             production_story_action=action,story_report=identity if action is not None else None)
            assert events==[]
        else:
            assert (await canary._run_scoped_job(scope,production_reply_check=action is None,
                                               production_story_action=action,story_report=identity if action is not None else None)).engaged==1
            assert events==['pool','mcp_ready','mcp_closed']
    assert sql(world,'SELECT status,attempts FROM jobs WHERE id=%s',(other,))==[('queued',0)]
    assert sql(world,'SELECT status FROM hermes_execution_runs WHERE id=%s',(world['run_id'],))==[('running',)]
    assert sql(world,'SELECT status,attempts FROM jobs WHERE id=%s',(scope.job_id,))==[
        ('queued',0) if collision else ('completed',1)]


@pytest.mark.parametrize('action',[Action.VIEW,Action.LIKE])
@pytest.mark.parametrize('sources,capacity',[(True,1),(False,1),(True,0)])
async def test_story_selection_clamps_saved_budget_and_requires_source(action,sources,capacity):
    job=claimed_job()
    scope=CanaryScope(job.user_id,job.ig_account_id,job.id,job.claimed_by)
    original=ActionBudget(capacity,5,5,capacity,views=capacity,private_messages=10)
    context=replace(run_context(),budget=original,config_snapshot={'follower_sources':[{}] if sources else []})
    loader=canary.ProductionStoryContextLoader(FakeLoader(context),scope,action)
    if not sources or not capacity:
        with pytest.raises(canary.RunContextError,match='approved_follower_sources_required|budget_exhausted'):
            await loader.load(job)
    else:
        loaded=await loader.load(job)
        assert loaded.budget==ActionBudget(int(action is Action.LIKE),0,0,int(action is Action.LIKE),
                                          views=int(action is Action.VIEW),private_messages=0)
        snapshot=loaded.config_snapshot['production_check']
        assert snapshot['selected_action']==action.value and snapshot['new_prospect_only'] is True
        assert snapshot['effective_budget']==asdict(loaded.budget)
        assert snapshot['original_budget']==asdict(original) and 'canary' not in loaded.config_snapshot
        assert context.budget is original and 'production_check' not in context.config_snapshot


async def test_view_uses_only_saved_view_allowance_without_plan_engagement_capacity():
    job=claimed_job()
    scope=CanaryScope(job.user_id,job.ig_account_id,job.id,job.claimed_by)
    context=replace(run_context(),budget=ActionBudget(9,9,9,0,views=10),
                    config_snapshot={'follower_sources':[{}]})
    loaded=await canary.ProductionStoryContextLoader(FakeLoader(context),scope,Action.VIEW).load(job)
    assert loaded.budget.views==1 and loaded.budget.engagements==0
    with pytest.raises(canary.RunContextError,match='budget_exhausted'):
        await canary.ProductionStoryContextLoader(FakeLoader(context),scope,Action.LIKE).load(job)


@pytest.mark.parametrize('action',[True,False,1,'view','like',Action.REPLY,Action.DM])
async def test_story_check_refuses_invalid_action_before_runtime(action,monkeypatch):
    monkeypatch.setattr(production_check,'_run_scoped_job',lambda *a,**k:pytest.fail('runtime touched'))
    with pytest.raises(CanaryError,match='invalid_production_story_action'):
        await production_check.run_story(CanaryScope(uuid4(),uuid4(),uuid4(),'check'),action)


@pytest.mark.parametrize('reply,production_reply',[(True,False),(False,True)])
async def test_story_selection_cannot_combine_reply_modes(reply,production_reply):
    with pytest.raises(CanaryError,match='invalid_production_story_action'):
        await canary._run_scoped_job(CanaryScope(uuid4(),uuid4(),uuid4(),'check',reply),
                                    production_reply_check=production_reply,production_story_action=Action.VIEW)


def story_receipt(w,action):
    from psycopg.types.json import Jsonb
    from tests.worker.test_canary_scripts import confirmed_action, NOW as clock
    _,engagement=confirmed_action(w,action=action.value)
    scope=CanaryScope(w['user_id'],w['account_id'],w['job_id'],'check')
    sql(w,"UPDATE jobs SET status='completed',attempts=1,claimed_by=%s WHERE id=%s",(scope.worker_id,scope.job_id))
    sql(w,"""UPDATE hermes_execution_runs SET status='completed',started_at=%s,completed_at=%s,
        config_snapshot=%s WHERE id=%s""",(clock-timedelta(minutes=6),clock,Jsonb({'production_check':{
        'job_id':str(scope.job_id),'worker_id':scope.worker_id,'execution_mode':'autonomous',
        'selected_action':action.value,'new_prospect_only':True}}),w['run_id']))
    sql(w,"UPDATE hermes_tool_calls SET completed_at=%s,safe_result=%s WHERE run_id=%s",
        (clock-timedelta(minutes=2),Jsonb({'status':'confirmed','evidence':
         f'action={action.value},stage=closed,request=forwarded,receipt=accepted,http=200,health=none'}),w['run_id']))
    sql(w,'UPDATE run_summaries SET completed_at=%s WHERE job_id=%s',(clock,scope.job_id))
    return scope,engagement


def reply_receipt(w):
    """Write the full native receipt chain expected from a reply check."""
    from psycopg.types.json import Jsonb
    from tests.worker.test_canary_scripts import TEXT, TEXT_HASH, confirmed_action, NOW as clock

    _, engagement = confirmed_action(w, action="reply")
    scope = CanaryScope(w['user_id'], w['account_id'], w['job_id'], 'check', reply_only=True)
    sql(w, "UPDATE jobs SET status='completed',attempts=1,claimed_by=%s WHERE id=%s",
        (scope.worker_id, scope.job_id))
    sql(w, """UPDATE hermes_execution_runs SET status='completed',started_at=%s,completed_at=%s,
        config_snapshot=%s WHERE id=%s""", (clock-timedelta(minutes=6), clock, Jsonb({'production_check': {
        'job_id': str(scope.job_id), 'worker_id': scope.worker_id,
        'execution_mode': 'autonomous', 'reply_only': True, 'new_prospect_only': True}}), w['run_id']))
    sql(w, "UPDATE hermes_tool_calls SET completed_at=%s,proposed_text=%s,generated_text_hash=%s,safe_result=%s WHERE run_id=%s",
        (clock-timedelta(minutes=2), TEXT, TEXT_HASH, Jsonb({'status': 'confirmed', 'evidence':
         'action=reply,stage=closed,request=forwarded,receipt=accepted,http=200,health=none'}), w['run_id']))
    sql(w, "UPDATE story_engagements SET generated_text=%s WHERE id=%s", (TEXT, engagement))
    sql(w, 'UPDATE run_summaries SET completed_at=%s WHERE job_id=%s', (clock, scope.job_id))
    return scope, engagement


@pytest.mark.parametrize('action',[Action.VIEW,Action.LIKE])
@pytest.mark.parametrize('change',['none','foreign','wrong_action','uncertain','no_native_receipt','job_failed',
    'run_failed','attempted_twice','wrong_worker','extra_tool','wrong_summary','old_receipt','missing_usage',
    'released_usage','unfinalized_usage','missing_summary','missing_engagement','wrong_snapshot','unfinished_tool','paused',
    'unforwarded','native_rejected','native_wrong_action','native_http_failed'])
def test_story_receipt_requires_exact_completed_selected_chain(world,action,change):  # noqa: F811
    w=world
    scope,engagement=story_receipt(w,action)
    if change=='foreign':
        scope=replace(scope,user_id=w['other_user_id'])
    elif change=='wrong_action':
        action=Action.LIKE if action is Action.VIEW else Action.VIEW
    elif change in ('uncertain','no_native_receipt'):
        sql(w,"UPDATE hermes_tool_calls SET "+("status='uncertain'" if change=='uncertain' else "safe_result='{}'")+" WHERE run_id=%s",(w['run_id'],))
    elif change in ('job_failed','attempted_twice','wrong_worker'):
        sql(w,'UPDATE jobs SET '+{'job_failed':"status='failed'",'attempted_twice':'attempts=2','wrong_worker':"claimed_by='other'"}[change]+' WHERE id=%s',(scope.job_id,))
    elif change=='run_failed':
        sql(w,"UPDATE hermes_execution_runs SET status='failed' WHERE id=%s",(w['run_id'],))
    elif change=='extra_tool':
        sql(w,"INSERT INTO hermes_tool_calls (run_id,tool_name,idempotency_key,status) VALUES (%s,'view','extra-check-0001','denied')",(w['run_id'],))
    elif change=='wrong_summary':
        sql(w,'UPDATE run_summaries SET replies_sent=1 WHERE job_id=%s',(scope.job_id,))
    elif change=='old_receipt':
        sql(w,"UPDATE story_engagements SET executed_at='2020-01-01' WHERE id=%s",(engagement,))
    elif change=='missing_summary':
        sql(w,'DELETE FROM run_summaries WHERE job_id=%s',(scope.job_id,))
    elif change=='missing_engagement':
        sql(w,'DELETE FROM story_engagements WHERE id=%s',(engagement,))
    elif change=='wrong_snapshot':
        sql(w,"UPDATE hermes_execution_runs SET config_snapshot='{}' WHERE id=%s",(w['run_id'],))
    elif change=='unfinished_tool':
        sql(w,'UPDATE hermes_tool_calls SET completed_at=NULL WHERE run_id=%s',(w['run_id'],))
    elif change=='paused':
        sql(w,'UPDATE ig_accounts SET paused_at=now() WHERE id=%s',(scope.account_id,))
    elif change in ('unforwarded','native_rejected','native_wrong_action','native_http_failed'):
        field,value={'unforwarded':('forwarded','not_forwarded'),'native_rejected':('accepted','missing'),
                     'native_wrong_action':(f'action={action.value}','action=reply'),
                     'native_http_failed':('http=200','http=503')}[change]
        sql(w,"UPDATE hermes_tool_calls SET safe_result=jsonb_set(safe_result,'{evidence}',to_jsonb(replace(safe_result->>'evidence',%s,%s))) WHERE run_id=%s",(field,value,w['run_id']))
    elif change in ('missing_usage','released_usage','unfinalized_usage'):
        if action is Action.VIEW:
            sql(w,"INSERT INTO engagement_usage_events (user_id,ig_account_id,engagement_id,action,status) VALUES (%s,%s,%s,'like','reserved')",
                (scope.user_id,scope.account_id,engagement))
        elif change=='missing_usage':
            sql(w,'DELETE FROM engagement_usage_events WHERE engagement_id=%s',(engagement,))
        elif change=='released_usage':
            sql(w,"UPDATE engagement_usage_events SET status='released' WHERE engagement_id=%s",(engagement,))
        else:
            sql(w,'UPDATE engagement_usage_events SET finalized_at=NULL WHERE engagement_id=%s',(engagement,))
    before=sql(w,'SELECT * FROM jobs ORDER BY id')
    receipt=production_check._selected_story_receipt(w['pool'],scope,action)
    assert (receipt is not None)==(change=='none')
    if receipt:
        assert receipt['engagement_id']==engagement and receipt['run_id']==w['run_id']
    assert sql(w,'SELECT * FROM jobs ORDER BY id')==before


@pytest.mark.parametrize('change', ['none', 'missing', 'unforwarded', 'tenant_mismatch', 'wrong_hash'])
def test_reply_receipt_requires_exact_native_forwarded_chain(world, change):  # noqa: F811
    w = world
    scope, engagement = reply_receipt(w)
    if change == 'missing':
        sql(w, 'DELETE FROM hermes_tool_calls WHERE run_id=%s', (w['run_id'],))
    elif change == 'unforwarded':
        sql(w, "UPDATE hermes_tool_calls SET safe_result=jsonb_set(safe_result,'{evidence}',to_jsonb(replace(safe_result->>'evidence','forwarded','not_forwarded'))) WHERE run_id=%s", (w['run_id'],))
    elif change == 'tenant_mismatch':
        scope = replace(scope, user_id=w['other_user_id'])
    elif change == 'wrong_hash':
        sql(w, "UPDATE hermes_tool_calls SET generated_text_hash=%s WHERE run_id=%s", ('0' * 64, w['run_id']))
    before = sql(w, 'SELECT * FROM jobs ORDER BY id')
    receipt = production_check._selected_reply_receipt(w['pool'], scope)
    assert (receipt is not None) == (change == 'none')
    if receipt:
        assert receipt['engagement_id'] == engagement and receipt['run_id'] == w['run_id']
    assert sql(w, 'SELECT * FROM jobs ORDER BY id') == before


@pytest.mark.parametrize('action',[Action.VIEW,Action.LIKE])
@pytest.mark.parametrize('valid',[True,False])
@pytest.mark.parametrize('identity',['missing','matching','foreign'])
async def test_story_check_success_uses_receipt_even_when_view_engaged_is_zero(world,monkeypatch,action,valid,identity):  # noqa: F811
    from engagemate import config
    from engagemate.db import pool
    from engagemate.worker.runner import RunSummary
    scope,_=story_receipt(world,action)
    if not valid:
        sql(world,"UPDATE hermes_tool_calls SET status='uncertain' WHERE run_id=%s",(world['run_id'],))
    with world['pool'].connection() as connection:
        native=connection.execute('SELECT target_id,opportunity_id,story_fingerprint FROM hermes_tool_calls WHERE run_id=%s',
                                  (world['run_id'],)).fetchone()
    observed={'owner_id':'123456','media_id':'987654_123456',**native}
    if identity=='foreign':
        observed['target_id']=uuid4()
    async def run(exact,**values):
        assert exact==scope and values['production_story_action'] is action
        assert values['story_report']=={}
        if identity!='missing':
            values['story_report'].update(observed)
        return RunSummary(scope.job_id,scope.user_id,scope.account_id,JobStatus.COMPLETED,1,0,0,0,0,'completed')
    monkeypatch.setattr(production_check,'_run_scoped_job',run)
    monkeypatch.setattr(config,'Settings',lambda:NS(database_url='fixture'))
    monkeypatch.setattr(pool,'create_pool',lambda *a,**k:BorrowedPool(world['pool']))
    result=await production_check.run_story(scope,action)
    assert result['verified'] is valid and result['summary']['engaged']==0
    assert result['story_identity']==(observed if valid and identity=='matching' else None)


@pytest.mark.parametrize('action',['view','like'])
@pytest.mark.parametrize('verified',[True,False])
def test_story_cli_returns_success_only_for_selected_receipt(action,verified,monkeypatch,capsys):
    async def run(scope,selected,**values):
        assert selected is Action(action) and scope.reply_only is False and values['timeout_seconds']==60
        return {'verified':verified,'selected_action':action,'receipt':{'engagement_id':str(uuid4())} if verified else None}
    monkeypatch.setattr(production_check,'run_story',run)
    code=production_check.main([action,'--user-id',str(uuid4()),'--account-id',str(uuid4()),
        '--job-id',str(uuid4()),'--worker-id','check','--timeout-seconds','60'])
    assert code==(0 if verified else 3)
    assert 'selected_action' in capsys.readouterr().out


@pytest.mark.parametrize('verified', [True, False])
def test_reply_cli_returns_success_only_for_verified_receipt(verified, monkeypatch, capsys):
    async def run(scope, **values):
        assert scope.reply_only is True and values['timeout_seconds'] == 60
        return {'verified': verified, 'selected_action': 'reply',
                'receipt': {'engagement_id': str(uuid4())} if verified else None}

    monkeypatch.setattr(production_check, 'run_reply', run)
    code = production_check.main(['reply', '--user-id', str(uuid4()), '--account-id', str(uuid4()),
        '--job-id', str(uuid4()), '--worker-id', 'check', '--timeout-seconds', '60'])
    assert code == (0 if verified else 3)
    assert 'selected_action' in capsys.readouterr().out


def identity_run():
    from engagemate.worker.active_run import ActiveRunRegistry
    from engagemate.worker.story_fingerprint import fingerprint_story
    from engagemate.worker.story_types import StoryOpportunity
    job=claimed_job()
    context=replace(run_context(),user_id=job.user_id,ig_account_id=job.ig_account_id)
    run_id,opportunity_id,target_id=uuid4(),uuid4(),uuid4()
    fingerprint=fingerprint_story('123456','987654_123456',NOW,None)
    measured=StoryOpportunity(target_id,'123456','target','vip','987654_123456',NOW,fingerprint,
                              NS(private='never-print-private-content'))
    active=NS(run_id=run_id,job=job,context=context,opportunities={opportunity_id:measured})
    values={'run_id':run_id,'job_id':job.id,'user_id':job.user_id,'ig_account_id':job.ig_account_id,
            'worker_id':job.claimed_by,'opportunity_id':opportunity_id,'target_id':target_id,
            'story_fingerprint':fingerprint}
    return ActiveRunRegistry(),active,values,measured


@pytest.mark.parametrize('change',['none','inactive','missing','untyped','bad_owner','owner_newline','owner_url',
    'missing_media','bad_media','media_url','wrong_target','wrong_fingerprint','forged_fingerprint','naive_clock',
    'foreign_run','foreign_job','foreign_user','foreign_account','foreign_worker','untyped_target','untyped_opportunity'])
def test_measured_identity_binds_native_ids_to_exact_registered_story(change,capsys):
    registry,active,values,measured=identity_run()
    if change.startswith('foreign_'):
        field={'run':'run_id','job':'job_id','user':'user_id','account':'ig_account_id','worker':'worker_id'}[change[8:]]
        values[field]='foreign' if field=='worker_id' else uuid4()
    elif change=='missing':
        active.opportunities={}
    elif change=='untyped':
        active.opportunities[values['opportunity_id']]=NS(**asdict(measured))
    elif change in ('bad_owner','owner_newline','owner_url','missing_media','bad_media','media_url',
                    'wrong_target','wrong_fingerprint','forged_fingerprint','naive_clock','untyped_target'):
        mutations={'bad_owner':{'owner_id':True},'owner_newline':{'owner_id':'123456\n'},
            'owner_url':{'owner_id':'https://example.test/'},'missing_media':{'media_id':None},
            'bad_media':{'media_id':1},'media_url':{'media_id':'https://example.test/private'},
            'wrong_target':{'target_id':uuid4()},'wrong_fingerprint':{'fingerprint':'b'*64},
            'forged_fingerprint':{'fingerprint':'c'*64},'naive_clock':{'taken_at':NOW.replace(tzinfo=None)},
            'untyped_target':{'target_id':str(measured.target_id)}}
        active.opportunities[values['opportunity_id']]=replace(measured,**mutations[change])
        if change in ('forged_fingerprint','untyped_target'):
            values['story_fingerprint' if change=='forged_fingerprint' else 'target_id']=mutations[change][
                'fingerprint' if change=='forged_fingerprint' else 'target_id']
    elif change=='untyped_opportunity':
        active.opportunities={str(values['opportunity_id']):measured}
        values['opportunity_id']=str(values['opportunity_id'])
    with registry.activate(active):
        result=canary._story_identity(registry,values) if change!='inactive' else None
    if change=='inactive':
        result=canary._story_identity(registry,values)
    if change=='none':
        assert result=={'owner_id':measured.owner_id,'media_id':measured.media_id,
                       **{key:values[key] for key in ('target_id','opportunity_id','story_fingerprint')}}
        assert 'private' not in repr(result)
    else:
        assert result is None
    captured=capsys.readouterr()
    assert captured.out==captured.err==''


@pytest.mark.parametrize('report',[True,[],{'owner_id':'injected'}])
async def test_operator_report_refuses_untyped_or_prefilled_container_before_runtime(report):
    with pytest.raises(CanaryError,match='invalid_production_story_report'):
        await canary._run_scoped_job(CanaryScope(uuid4(),uuid4(),uuid4(),'check'),
                                    production_story_action=Action.VIEW,story_report=report)


async def test_report_cannot_be_added_to_ordinary_canary():
    with pytest.raises(CanaryError,match='invalid_production_story_report'):
        await canary._run_scoped_job(CanaryScope(uuid4(),uuid4(),uuid4(),'check'),story_report={})
