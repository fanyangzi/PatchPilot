from ..domain import SkillManifest, TaskSpec
class PolicyGate:
    def check(self,skill:SkillManifest,task:TaskSpec,write_paths=None):
        if task.risk_policy.get("network") is False and any("network" in t for t in skill.tools): return False,"NETWORK_DISABLED"
        if skill.risk=="high" and not task.risk_policy.get("allow_high_risk",True): return False,"HIGH_RISK_SKILL_DISABLED"
        if write_paths and task.risk_policy.get("allowed_paths"):
            allowed=task.risk_policy["allowed_paths"]
            bad=[p for p in write_paths if not any(p.startswith(a) for a in allowed)]
            if bad:return False,f"PATH_SCOPE:{','.join(bad)}"
        return True,"ALLOW"
