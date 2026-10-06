from __future__ import annotations
import json, os, re, urllib.request, urllib.error
from pathlib import Path

class RemoteModelConfig:
    def __init__(self,base_url=None,api_key=None,model=None,timeout=None):
        vals={}
        p=Path('.env.local')
        if p.exists():
            for line in p.read_text().splitlines():
                line=line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k,v=line.split('=',1); vals[k.strip()]=v.strip().strip('"\'')
        self.base_url=base_url or os.getenv('BASE_URL') or vals.get('BASE_URL','')
        self.api_key=api_key or os.getenv('API_KEY') or vals.get('API_KEY','')
        self.model=model or os.getenv('MODEL') or vals.get('MODEL','')
        self.timeout=float(timeout or os.getenv('REQUEST_TIMEOUT_SEC') or vals.get('REQUEST_TIMEOUT_SEC','30'))
    def public(self): return {'base_url':self.base_url,'model':self.model,'configured':bool(self.base_url and self.api_key)}

class RemoteModel:
    def __init__(self,config=None): self.config=config or RemoteModelConfig(); self.last_usage={}
    def _url(self): return self.config.base_url.rstrip('/')+'/chat/completions'
    def chat(self,messages,temperature=0.1,json_mode=False):
        if not self.config.base_url or not self.config.api_key: raise RuntimeError('Remote model is not configured; set BASE_URL/API_KEY in .env.local')
        body={'model':self.config.model,'messages':messages,'temperature':temperature}
        if json_mode: body['response_format']={'type':'json_object'}
        req=urllib.request.Request(self._url(),data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+self.config.api_key},method='POST')
        try:
            with urllib.request.urlopen(req,timeout=self.config.timeout) as r: data=json.loads(r.read())
        except urllib.error.HTTPError as e: raise RuntimeError(f'model request failed HTTP {e.code}') from e
        if 'usage' in data: self.last_usage = data['usage']
        return data['choices'][0]['message']['content']
    def doctor(self):
        return self.chat([{'role':'user','content':'Return JSON exactly: {"status":"ok"}'}],temperature=0,json_mode=True)

    def plan_task(self, *, task_id: str, issue_title: str, issue_body: str,
                  repo_tree: list[str], candidate_skills: list[str]) -> dict:
        """Ask the configured model for a bounded maintenance plan.

        The model is advisory: the orchestrator still validates the returned
        JSON, applies its own policy gate, and executes only deterministic
        allow-listed fixture actions.  We intentionally return a small,
        redacted structure rather than persisting the raw model response.
        """
        tree = "\n".join(repo_tree[:80])
        skills = ", ".join(candidate_skills[:12])
        prompt = (
            "You are PatchPilot's maintenance planner. Return JSON only with "
            "keys summary, suggested_skills, risk_flags, acceptance_checks. "
            "Do not propose shell commands or secrets. Keep each string short.\n"
            f"task_id: {task_id}\nissue_title: {issue_title}\n"
            f"issue_body: {issue_body[:1800]}\n"
            f"repository tree:\n{tree}\n"
            f"candidate skills: {skills}"
        )
        raw = self.chat([{'role': 'system', 'content': 'You produce bounded JSON plans for a code maintenance runtime.'},
                         {'role': 'user', 'content': prompt}], temperature=0, json_mode=True)
        # Some OpenAI-compatible gateways wrap JSON in a markdown fence even
        # when response_format was requested.  Parse the first object safely.
        text = raw.strip()
        if text.startswith('```'):
            text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.I | re.S).strip()
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find('{'), text.rfind('}')
            if start < 0 or end <= start:
                raise RuntimeError('remote planner returned non-JSON output')
            value = json.loads(text[start:end + 1])
        if not isinstance(value, dict):
            raise RuntimeError('remote planner returned a non-object')
        return {
            'summary': str(value.get('summary', ''))[:500],
            'suggested_skills': [str(x)[:80] for x in value.get('suggested_skills', []) if isinstance(x, (str, int))][:8],
            'risk_flags': [str(x)[:120] for x in value.get('risk_flags', []) if isinstance(x, (str, int))][:8],
            'acceptance_checks': [str(x)[:160] for x in value.get('acceptance_checks', []) if isinstance(x, (str, int))][:8],
        }

    def propose_patch(self, task, repo_files: dict, failed_tests: list) -> str:
        files_ctx = '\n\n'.join(f'=== {p} ===\n{c[:2000]}' for p, c in list(repo_files.items())[:10])
        tests_ctx = 'Failed tests:\n' + '\n'.join(f'- {t}' for t in failed_tests[:10])
        prompt = (
            'You are a code repair assistant. Generate a unified diff patch to fix the issue.\n\n'
            f'Issue: {task.issue_title}\n{task.issue_body[:1000]}\n\n'
            f'{tests_ctx}\n\nRepository files:\n{files_ctx}\n\n'
            'Return ONLY a valid unified diff (--- a/path +++ b/path format). No markdown fences.'
        )
        raw = self.chat([
            {'role': 'system', 'content': 'You generate unified diff patches for code fixes.'},
            {'role': 'user', 'content': prompt}
        ], temperature=0.2)
        diff = raw.strip()
        if diff.startswith('```'):
            diff = re.sub(r'^```(?:diff)?\s*|\s*```$', '', diff, flags=re.I | re.S).strip()
        return diff

    def get_estimated_cost(self) -> float:
        if not self.last_usage: return 0.0
        p = self.last_usage.get('prompt_tokens', 0)
        c = self.last_usage.get('completion_tokens', 0)
        return round((p * 0.03 + c * 0.06) / 1000, 6)
