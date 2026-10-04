import json
from pathlib import Path
from patchpilot.domain import TaskSpec, FailureClass
from patchpilot.skills import SkillRegistry, GraphRouter, PolicyGate
from patchpilot.verifier import Verifier

def test_registry_and_router():
    reg=SkillRegistry(); assert len(reg.all()) >= 9; assert reg.validate()==[]
    task=TaskSpec('x','repo','fix parser regression','parser regression test')
    graph=GraphRouter(reg).route(task); assert graph['selected']; assert graph['nodes'][0]['score'] >= graph['nodes'][-1]['score']

def test_policy_and_verifier():
    task=TaskSpec('x','repo','x','x',risk_policy={'allowed_paths':['src/'],'network':False})
    assert PolicyGate().check(reg:=reg_or_skill(),task,['tests/a.py'])[0] is False
    result=Verifier().verify(True,True,True,['src/a.py'],task,True); assert result.status=='PASSED'
    blocked=Verifier().verify(True,True,True,['release/a.py'],task,True); assert blocked.status=='BLOCKED'

def reg_or_skill(): return SkillRegistry().get('propose_patch')
