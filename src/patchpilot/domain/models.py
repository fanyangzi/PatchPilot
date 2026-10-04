from __future__ import annotations
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any
import hashlib, json, time, uuid

class RunStatus(str, Enum):
    RECEIVED="RECEIVED"; SNAPSHOT_READY="SNAPSHOT_READY"; PLAN_READY="PLAN_READY"
    REPRODUCING="REPRODUCING"; REPRODUCED="REPRODUCED"; PATCHING="PATCHING"
    LOCATED="LOCATED"; COMPLIANCE_CHECKED="COMPLIANCE_CHECKED"
    VERIFYING="VERIFYING"; PASSED="PASSED"; FAILED="FAILED"; DIAGNOSED="DIAGNOSED"
    RECOVERING="RECOVERING"; RETRYING="RETRYING"; BLOCKED="BLOCKED"; NEEDS_REVIEW="NEEDS_REVIEW"
    EVIDENCE_PACKED="EVIDENCE_PACKED"

class FailureClass(str, Enum):
    REPRO_NOT_CONFIRMED="REPRO_NOT_CONFIRMED"; TEST_ASSERTION="TEST_ASSERTION"
    DEPENDENCY_CONFLICT="DEPENDENCY_CONFLICT"; PATCH_CONFLICT="PATCH_CONFLICT"
    TIMEOUT="TIMEOUT"; REGRESSION="REGRESSION"; LICENSE_OR_RISK="LICENSE_OR_RISK"
    PROMPT_INJECTION="PROMPT_INJECTION"; UNKNOWN="UNKNOWN"

@dataclass
class TaskSpec:
    task_id: str
    repo: str
    issue_title: str
    issue_body: str
    commit: str = "working-tree"
    expected: list[str] = field(default_factory=lambda:["reproduce","patch","regression_test"])
    constraints: dict[str,Any] = field(default_factory=lambda:{"language":"python","test_command":"pytest -q"})
    risk_policy: dict[str,Any] = field(default_factory=lambda:{"network":False,"max_runtime_sec":180,"allowed_paths":["src/","tests/"]})
    scenario: str = "normal"
    @classmethod
    def from_dict(cls, data:dict[str,Any]):
        return cls(task_id=str(data["task_id"]), repo=str(data["repo"]), issue_title=str(data["issue_title"]), issue_body=str(data.get("issue_body","")), commit=str(data.get("commit","working-tree")), expected=list(data.get("expected",["reproduce","patch","regression_test"])), constraints=dict(data.get("constraints",{})), risk_policy=dict(data.get("risk_policy",{})), scenario=str(data.get("scenario","normal")))
    def to_dict(self): return asdict(self)

@dataclass
class SkillManifest:
    name:str; version:str="0.1.0"; inputs:list[str]=field(default_factory=list); outputs:list[str]=field(default_factory=list)
    preconditions:list[str]=field(default_factory=list); tools:list[str]=field(default_factory=list); risk:str="low"; cost:float=1.0
    rollback:str="none"; success_signals:list[str]=field(default_factory=list); relevance_keywords:list[str]=field(default_factory=list)
    def to_dict(self): return asdict(self)

@dataclass
class FailureState:
    failure_class:FailureClass; message:str; recovery_actions:list[str]=field(default_factory=list); attempt:int=0
    def to_dict(self):
        d=asdict(self); d["failure_class"]=self.failure_class.value; return d

@dataclass
class VerifierResult:
    status:str; confidence:float; checks:dict[str,Any]; failure:FailureState|None=None
    def to_dict(self):
        d=asdict(self); d["failure"]=self.failure.to_dict() if self.failure else None; return d

@dataclass
class Evidence:
    evidence_id:str; kind:str; title:str; status:str; payload:dict[str,Any]=field(default_factory=dict); parents:list[str]=field(default_factory=list); created_at:float=field(default_factory=time.time)
    def to_dict(self): return asdict(self)

@dataclass
class Artifact:
    artifact_id:str; kind:str; path:str; sha256:str; size:int; run_id:str; metadata:dict[str,Any]=field(default_factory=dict)
    def to_dict(self): return asdict(self)

@dataclass
class Event:
    run_id:str; event_type:str; status:str; message:str; data:dict[str,Any]=field(default_factory=dict); event_id:str=field(default_factory=lambda:uuid.uuid4().hex); ts:float=field(default_factory=time.time)
    def to_dict(self): return asdict(self)

@dataclass
class Run:
    run_id:str; task_id:str; status:RunStatus=RunStatus.RECEIVED; started_at:float=field(default_factory=time.time); ended_at:float|None=None
    conclusion:str|None=None; attempt:int=0; metrics:dict[str,Any]=field(default_factory=dict); artifacts:list[str]=field(default_factory=list)
    def to_dict(self):
        d=asdict(self); d["status"]=self.status.value; return d

def stable_hash(data:Any)->str:
    return hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()
