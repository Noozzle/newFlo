# Must follow
- Work inside existing repo. No new project from scratch.
- Do NOT change strategy entry/exit logic. Only add AI gate: SKIP/HALF/FULL (risk *= 0/0.5/1.0).
- No data leakage: features use only data <= candidate timestamp.
- Validation is walk-forward time split only (no shuffle).
- Make real file edits. Prefer small diffs.

# Data locations
See @.claude/rules/data-paths.md and @.claude/rules/ai-gate.md
