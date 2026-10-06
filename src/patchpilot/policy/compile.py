"""Compile repository policies from prose documents and config files."""
from __future__ import annotations
import re
import yaml
from pathlib import Path
from .schema import Policy, LicenseRule

def compile_policy(repo_path: str | Path) -> Policy:
    """Compile policy from repository's own declarations.

    Priority (first match wins):
    1. patchpilot.policy.yaml (explicit config)
    2. AI_POLICY.md (marked sections)
    3. CONTRIBUTING.md (marked sections)
    4. Default policy (permissive fallback)

    Returns a Policy instance. Raises ValueError if policy file exists but is invalid.
    """
    repo = Path(repo_path)

    # Try explicit config first
    policy_file = repo / 'patchpilot.policy.yaml'
    if policy_file.exists():
        return _from_yaml(policy_file)

    # Try AI_POLICY.md
    ai_policy = repo / 'AI_POLICY.md'
    if ai_policy.exists():
        policy = _from_markdown(ai_policy, source='AI_POLICY.md')
        if policy:
            return policy

    # Try CONTRIBUTING.md
    contributing = repo / 'CONTRIBUTING.md'
    if contributing.exists():
        policy = _from_markdown(contributing, source='CONTRIBUTING.md')
        if policy:
            return policy

    # Fallback to permissive default
    return Policy(source='default')

def _from_yaml(path: Path) -> Policy:
    """Parse explicit YAML policy file."""
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except Exception as e:
        raise ValueError(f"Failed to parse {path}: {e}")

    if not isinstance(data, dict):
        raise ValueError(f"Policy file {path} must contain a YAML object")

    # Extract fields
    policy = Policy(source=str(path.name))

    if 'scope' in data:
        policy.scope = data['scope'] if isinstance(data['scope'], list) else [data['scope']]

    if 'allowed_paths' in data:
        policy.allowed_paths = data['allowed_paths'] if isinstance(data['allowed_paths'], list) else [data['allowed_paths']]

    if 'sensitive_patterns' in data:
        policy.sensitive_patterns = data['sensitive_patterns'] if isinstance(data['sensitive_patterns'], list) else [data['sensitive_patterns']]

    if 'required_checks' in data:
        policy.required_checks = data['required_checks']

    if 'max_attempts' in data:
        policy.max_attempts = int(data['max_attempts'])

    if 'license' in data:
        lic_data = data['license']
        policy.license = LicenseRule(
            allowed_licenses=lic_data.get('allowed_licenses', policy.license.allowed_licenses),
            forbidden_licenses=lic_data.get('forbidden_licenses', policy.license.forbidden_licenses),
            require_license_file=lic_data.get('require_license_file', True)
        )

    return policy

def _from_markdown(path: Path, source: str) -> Policy | None:
    """Extract policy from marked sections in markdown.

    Looks for fenced code blocks tagged with ```patchpilot-policy
    """
    try:
        text = path.read_text()
    except Exception:
        return None

    # Find fenced code blocks with patchpilot-policy tag
    pattern = r'```patchpilot-policy\s*\n(.*?)\n```'
    matches = re.findall(pattern, text, re.DOTALL)

    if not matches:
        return None

    # Parse the first match as YAML
    try:
        data = yaml.safe_load(matches[0])
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    # Construct policy using _from_yaml logic but with dict input
    policy = Policy(source=source)

    if 'scope' in data:
        policy.scope = data['scope'] if isinstance(data['scope'], list) else [data['scope']]

    if 'allowed_paths' in data:
        policy.allowed_paths = data['allowed_paths'] if isinstance(data['allowed_paths'], list) else [data['allowed_paths']]

    if 'sensitive_patterns' in data:
        policy.sensitive_patterns = data['sensitive_patterns'] if isinstance(data['sensitive_patterns'], list) else [data['sensitive_patterns']]

    if 'required_checks' in data:
        policy.required_checks = data['required_checks']

    if 'max_attempts' in data:
        policy.max_attempts = int(data['max_attempts'])

    if 'license' in data:
        lic_data = data['license']
        policy.license = LicenseRule(
            allowed_licenses=lic_data.get('allowed_licenses', policy.license.allowed_licenses),
            forbidden_licenses=lic_data.get('forbidden_licenses', policy.license.forbidden_licenses),
            require_license_file=lic_data.get('require_license_file', True)
        )

    return policy
