from pathlib import Path
import json

from app.jobs.replay_deletions import replay
from tests.test_service import record,plan


async def test_replay_preserves_deletions_and_rebases(env,tmp_path):
    s,ctx,car,_=env
    await plan(s,ctx,car)
    r=await record(s,ctx,car)
    p=(await s.plan_list(ctx,car['id']))[0]
    journal=tmp_path/'separate'
    journal.mkdir()
    (journal/'2026-09-24.jsonl').write_text(json.dumps({'kind':'record','id':str(r['id'])})+'\n',encoding='utf-8')
    await replay(s,journal)
    assert not (await s.record_list(ctx,car['id']))['items']
    new=(await s.plan_list(ctx,car['id']))[0]
    assert new['last_record_id'] is None and new['cycle_no']==p['cycle_no']+1
    await replay(s,journal)
    assert (await s.plan_list(ctx,car['id']))[0]['cycle_no']==new['cycle_no']
