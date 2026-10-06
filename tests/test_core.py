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
    task=TaskSpec('x',str(Path(__file__).parent.parent),'x','x',risk_policy={'allowed_paths':['src/'],'network':False})
    assert PolicyGate().check(reg:=reg_or_skill(),task,['tests/a.py'])[0] is False

    # Verifier.verify() signature: (before_output, after_output, target_tests, diff_text, task)
    # Create mock pytest output
    before_out = "tests/test_foo.py::test_bar FAILED\n"
    after_out = "tests/test_foo.py::test_bar PASSED\n"
    target_tests = ["tests/test_foo.py::test_bar"]

    # Test with allowed path
    diff_allowed = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-old\n+new\n"
    result = Verifier().verify(before_out, after_out, target_tests, diff_allowed, task)
    assert result.status == 'PASSED'

    # Test with disallowed path
    diff_blocked = "diff --git a/release/a.py b/release/a.py\n--- a/release/a.py\n+++ b/release/a.py\n@@ -1 +1 @@\n-old\n+new\n"
    blocked = Verifier().verify(before_out, after_out, target_tests, diff_blocked, task)
    assert blocked.status == 'BLOCKED'

def reg_or_skill(): return SkillRegistry().get('propose_patch')
