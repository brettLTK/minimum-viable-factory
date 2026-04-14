# Forge Post-Build Testing Protocol — Automatic Execution

After every build is complete and before opening a merge, you MUST execute the Post-Build Testing Protocol defined in `factory/post-build-testing-protocol.md` in the build's output repository.

## What to do

1. Open `factory/post-build-testing-protocol.md` in the workspace and execute all 5 gates in order:
   - Gate 1: Smoke Test
   - Gate 2: AC Check
   - Gate 3: Eval Score
   - Gate 4: Observability Check
   - Gate 5: Documentation Gate

2. For each gate, record your verdict and evidence per the instructions in the protocol document.

3. After all 5 gates are evaluated, write a conforming `PostBuildTestRecord` YAML file to:
   ```
   factory/memory/test-records/YYYY-MM-DD-<ticket-slug>.yaml
   ```
   using the schema defined in the protocol document.

4. Commit the YAML record to the build branch before opening the merge/PR.

5. Do NOT open a merge/PR if the overall verdict is **NEEDS WORK**. Fix the blocking issue and re-run the full protocol.

## Why this matters

Builds that ship without a quality gate arrive as "correct code that has never run." The post-build testing protocol is the factory's primary mechanism for catching runtime errors, unverified acceptance criteria, and observability gaps before they reach main.

This instruction is always in effect. It cannot be overridden by build-specific prompts unless the ticket explicitly notes an exemption approved by the factory operator.