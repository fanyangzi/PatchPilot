from __future__ import annotations
from dataclasses import dataclass
from ..domain import SkillManifest

BUILTINS=[
("inspect_repository",["TaskSpec"],["RepoProfile"],["repo.read"],"low",.6,["repository","structure","python"]),
("search_symbol",["RepoProfile"],["LocationSet"],["repo.read"],"low",.7,["locate","symbol","search"]),
("inspect_dependencies",["RepoProfile"],["DependencySummary"],["repo.read"],"low",.5,["dependency","requirements","pyproject"]),
("reproduce_pytest_failure",["TaskSpec","RepoSnapshot"],["ReproductionResult"],["harness.run"],"medium",1.0,["reproduce","failure","test"]),
("generate_regression_test",["ReproductionResult"],["TestPatch"],["repo.write"],"medium",1.2,["regression","test"]),
("propose_patch",["LocationSet","ReproductionResult"],["Patch"],["repo.write"],"high",1.6,["patch","repair","fix"]),
("apply_patch_safely",["Patch"],["PatchedSnapshot"],["repo.write","git.write"],"high",1.0,["apply","patch"]),
("run_regression_suite",["PatchedSnapshot"],["VerifierResult"],["harness.run"],"medium",1.0,["verify","regression","pytest"]),
("static_risk_scan",["Patch"],["RiskResult"],["repo.read"],"medium",.7,["risk","security","scope"]),
("license_sbom_scan",["RepoSnapshot"],["ComplianceResult"],["repo.read"],"low",.6,["license","sbom","compliance"]),
("diagnose_failure",["ExecutionTrace"],["FailureState"],["trace.read"],"low",.5,["diagnose","failure","recover"]),
("build_evidence_bundle",["Run"],["Artifact"],["artifact.write"],"low",.5,["evidence","bundle","report"]),
]

def builtin_manifests():
    out=[]
    for name,inputs,outputs,tools,risk,cost,keywords in BUILTINS:
        out.append(SkillManifest(name,inputs=inputs,outputs=outputs,preconditions=[],tools=tools,risk=risk,cost=cost,rollback="worktree_reset" if "write" in " ".join(tools) else "none",success_signals=[f"{name}_complete"],relevance_keywords=keywords))
    return out

class SkillRegistry:
    def __init__(self,manifests=None): self.skills={s.name:s for s in (manifests or builtin_manifests())}
    def get(self,name): return self.skills[name]
    def all(self): return list(self.skills.values())
    def validate(self):
        errors=[]
        for s in self.skills.values():
            for field in ("inputs","outputs","tools","success_signals"):
                if not getattr(s,field): errors.append(f"{s.name}.{field} empty")
        return errors
