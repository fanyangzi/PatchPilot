"""Policy schema definitions."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal

@dataclass
class LicenseRule:
    """License compatibility rules."""
    allowed_licenses: list[str] = field(default_factory=lambda: ['MIT', 'Apache-2.0', 'BSD-3-Clause', 'BSD-2-Clause'])
    forbidden_licenses: list[str] = field(default_factory=lambda: ['GPL-3.0', 'AGPL-3.0'])
    require_license_file: bool = True

@dataclass
class Policy:
    """Compiled repository policy for patch verification.

    This represents the executable form of a repository's contribution rules,
    compiled from prose documents like CONTRIBUTING.md or explicit config files.
    """
    # Scope: which paths/files this policy applies to
    scope: list[str] = field(default_factory=lambda: ['**/*'])

    # Path restrictions: which paths patches are allowed to modify
    allowed_paths: list[str] = field(default_factory=list)

    # Sensitive patterns: paths that must not be touched
    sensitive_patterns: list[str] = field(default_factory=lambda: [
        '.github/workflows/**',
        '.gitlab-ci.yml',
        'Jenkinsfile',
        '**/*.key',
        '**/*.pem',
        '**/credentials.json',
        '**/.env',
        '**/secrets/**',
        '**/poetry.lock',
        '**/package-lock.json',
        '**/Pipfile.lock',
    ])

    # Required checks: which of the 6 checks must pass
    required_checks: list[Literal['reproduction', 'target_tests', 'regression_tests',
                                    'diff_scope', 'sensitive_paths', 'license_sbom']] = field(
        default_factory=lambda: ['target_tests', 'regression_tests', 'diff_scope',
                                  'sensitive_paths', 'license_sbom']
    )

    # Retry limits
    max_attempts: int = 3

    # License rules
    license: LicenseRule = field(default_factory=LicenseRule)

    # Source: where this policy came from
    source: str = 'default'

    def is_fail_closed(self) -> bool:
        """Return True if this is a fail-closed policy (strict enforcement)."""
        return bool(self.allowed_paths) or bool(self.sensitive_patterns)

    def requires_check(self, check_name: str) -> bool:
        """Return True if the given check is required by this policy."""
        return check_name in self.required_checks
