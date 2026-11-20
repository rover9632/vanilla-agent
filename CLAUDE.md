# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

