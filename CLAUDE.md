# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.


## Working agreements (developer's standing rules)

- Never edit `.env` or `.env.local` (secret files); update `.env.example` instead, and never print secret values from them.
- Commit messages carry NO `Co-Authored-By` or similar attribution trailers.
- Do not automatically commit, amend, squash, rebase, or push commits without my explicit authorization.
- Keep repository files publishable: no machine-local facts in tracked files (see "Documentation privacy and portability").
- Record decisions and portable requirements in repo files; keep decision rationale, investigation narratives, and review post-mortems in the conversation.


## Documentation privacy and portability

Anything committed may be read by people who have never seen this machine. Tracked files — including `docs/plan.md`, code comments, and commit messages — must not contain:

- hostnames, usernames, absolute paths outside the repo, or home-directory locations;
- IP addresses (including observed gateway / LAN / link-local / metadata addresses), network topology, or firewall details;
- resource specs of this box (RAM/CPU/core counts, disk sizes) or versions probed from it;
- local package-registry or container-registry mirror URLs, local image/container/tag names, local file names of unrelated projects;
- secret values, even partially redacted.

Write the **decision and the portable rule**, not the observation. `openai>=3.51` and `max_live_containers: 8` (sized as `containers × per-container memory ≲ 1/3 of host RAM`) carry no fingerprint; "measured here: that host timed out" does. Environment specifics belong in untracked files (`.env`, `config/*.local.yaml`) or the deploy host's own config, referenced from docs by variable name only.

When a fact genuinely requires an environment-dependent number, state it as a default plus the principle for recomputing it, and name the TODO/section that revalidates it on deploy. Before committing doc changes, grep them for the categories above.


## Planning and Task Management

1. **Plan Before Implementation**
   - Before starting a new project or modifying an existing one, review the current project state and create or update the implementation plan and TODO list.
   - If an existing plan is available, review and continue from it rather than creating a duplicate or starting over.
   - Keep the implementation plan and TODO list in `docs/plan.md`, which serves as the single source of truth for project planning and progress.
   - Keep `docs/plan.md` continuously synchronized with the actual project state.

2. **Keep TODO Items Small and Verifiable**
   - Break down large tasks into small, self-contained TODO items that can be implemented and verified independently.
   - Each TODO item should represent a meaningful, coherent unit of functionality with clear implementation goals and verifiable completion criteria.
   - Use hierarchical TODO items when necessary to break down complex tasks into manageable units.

3. **Update Progress Continuously**
   - Update `docs/plan.md` immediately after completing each TODO item, including its status, implementation details, validation results, and any newly identified work.
   - Ensure the TODO list always accurately reflects the actual implementation progress.

4. **Validate Every TODO Item**
   - After implementing each TODO item, run the relevant tests, checks, or other validation procedures.
   - Mark a TODO item as completed only after its implementation and validation are successful.
   - If validation fails, investigate and resolve the issues before marking the TODO item as completed.

5. **Require Explicit Approval for Each TODO Item**
   - Before starting the implementation of any TODO item, obtain my explicit approval.
   - Implement only one TODO item at a time. Do not start implementing another TODO item without my explicit instruction to proceed.
   - Approval of the overall plan does not authorize implementation of all TODO items. Each TODO item requires separate approval before implementation.


## Project status

The repo is a scaffold: `service/pyproject.toml` and an empty `service/tests/`. The README defines the goal: a lightweight, **framework-free** AI agent (no LangChain-style agent framework — direct provider SDK calls behind a small FastAPI service).

