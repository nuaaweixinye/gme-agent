---
name: gme-bug-fix
description: Fix GME production-code bugs exposed by selected generated GME vs ACIS comparison tests. Trace the public API across the selected business module and module/kernel, then edit exactly one repository that owns the first behavioral divergence.
---

# GME Bug Fix

## Operating Model

Work in a GME superproject repair worktree. The selected business module and `module/kernel` are available for source tracing. The test module identifies the build and test scope; it does not by itself identify the repository that owns the bug.

The selected failing GTest compares GME behavior against ACIS. Treat ACIS as the expected behavior unless the prompt or code clearly shows the generated test is invalid. Fix the smallest production-code issue that makes GME match ACIS for that one failing case.

The prompt lists the repair candidate repositories, normally `module/<module>` and `module/kernel`. Trace the public API wrapper, validation and outcome handling in kernel as well as the business implementation. Modify exactly one candidate repository: the repository containing the first implementation point that creates the observable mismatch. If a correct fix genuinely requires changes in more than one repository, stop and report that the repair must be split.

The generated test has already been copied into `tests/gme` by the agent only so the bug can be reproduced. That copied test is verification input, not part of the repair.

## Workflow

1. Read the failure id, test suite, test name, reason, GTest filter, reproduced failure output, generated test file, test module, and repair candidate repositories from the prompt.
2. Inspect the failing generated test, then locate the public GME API declaration and entry point named by `UniqueSymbol` or the failure context.
3. Follow the complete call chain from the public entry point to the internal function that performs the calculation or state change. Read the relevant parameter semantics, input normalization, boundary handling, return contract, error handling, and object ownership.
4. Compare the failing input through the GME and ACIS paths. Identify the first implementation point where their observable behavior diverges. Do not edit source before this root cause is understood.
5. Read adjacent implementations and existing tests to confirm the intended local convention and rule out an invalid test, undefined behavior, or an unreliable oracle.
6. Reproduce mentally from the code first; run the provided exact GTest filter when the environment is available.
7. Identify the narrowest implementation-level change that resolves the mismatch without special-casing the exact test data.
8. Select the one repository that owns the first divergence and modify only files in that repository using a localized patch. State the selected repository in the result.
9. Build the tests target and run the exact selected GTest filter. Repeat this fast loop until it passes or no safe production fix is possible.
10. Run clang-format validation on the edited C/C++ implementation files when the tool is available. If formatting is required, format only those edited files and review the resulting diff for unrelated churn.
11. Review the final diff and report the public API entry, key call chain, first divergence, root cause, fix, validation actually performed, and residual risk.

The GME Test Agent runs the authoritative final gates after Codex returns: changed-file scope validation, clang-format validation, the exact target test, the configured module's full functional test set, and a Release memory audit of the target test. Do not claim these coordinator-owned gates passed unless their output is actually available.

## Hard Rules

- Do not assume the test module owns the defect. Public API validation and outcome behavior may belong to `module/kernel`.
- Do not edit files outside the listed repair candidate repositories, and do not edit more than one candidate repository in a single repair task.
- Do not edit `include/` paths.
- Do not edit `tests/gme`, generated tests, fixtures, or any test file.
- Do not paper over a production mismatch by changing expected values, weakening assertions, deleting tests, or adding skips.
- Do not add `GTEST_SKIP`.
- Do not update submodule pointers manually.
- Do not make broad refactors while fixing a single comparison failure.
- Do not patch the public API exit with a hard-coded value for the observed test input.
- Do not bypass or duplicate an existing internal algorithm when the defect belongs in that algorithm.
- Do not modify source until the public entry point, key call chain, and first divergent implementation point have been identified.
- Preserve each edited file's existing encoding, BOM state, and CRLF/LF convention. Do not rewrite or transcode an entire file for a local fix.
- Do not format unrelated code or the whole repository. Keep formatting changes limited to edited implementation files.
- New comments must be concise Chinese comments and only explain non-obvious behavior. Do not add generated documentation blocks or bulk comments.

## Fix Quality

Prefer localized, behavior-preserving changes. Preserve existing API contracts, error handling style, and numerical/geometric tolerance conventions. If the ACIS behavior depends on edge-case parsing or geometry semantics, document the condition in code only when the local style already uses comments for similar cases.

Before returning, inspect the diff for accidental line-ending, encoding, comment, or formatting churn. If the interface contract cannot be established or ACIS behavior appears undefined, stop and explain why a safe production fix cannot be justified.
