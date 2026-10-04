from __future__ import annotations
import re
from ..domain import TaskSpec
from .registry import SkillRegistry

_RISK={"low":1,"medium":2,"high":3}
class GraphRouter:
    def __init__(self,registry=None): self.registry=registry or SkillRegistry()
    def route(self,task:TaskSpec,phase:str="all"):
        text=(task.issue_title+" "+task.issue_body).lower(); tokens=set(re.findall(r"[a-z_]{3,}",text)); rows=[]
        for skill in self.registry.all():
            relevance=min(1.0,len(tokens & set(skill.relevance_keywords))/max(1,len(skill.relevance_keywords)))
            pre=1.0
            evidence=min(1.0,.35+len(skill.outputs)*.12)
            risk=_RISK[skill.risk]/3; cost=min(1.0,skill.cost/2)
            score=.35*relevance+.20*pre+.20*.65+.15*evidence-.05*risk-.05*cost
            rows.append({"skill":skill.name,"score":round(score,4),"relevance":round(relevance,4),"precondition_match":pre,"evidence_gain":round(evidence,4),"risk":skill.risk,"tools":skill.tools})
        rows.sort(key=lambda x:x["score"],reverse=True); return {"phase":phase,"nodes":[r for r in rows],"selected":[r["skill"] for r in rows[:5]]}
