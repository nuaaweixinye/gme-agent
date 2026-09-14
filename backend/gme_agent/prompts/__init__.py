from __future__ import annotations

from .bug_fix import bug_fix_prompt
from .skip import skip_known_failure_prompt
from .test_generation import continue_test_generation_prompt, test_generation_prompt

__all__ = [
    "bug_fix_prompt",
    "continue_test_generation_prompt",
    "skip_known_failure_prompt",
    "test_generation_prompt",
]
