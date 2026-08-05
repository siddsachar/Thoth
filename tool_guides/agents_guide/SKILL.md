---
name: agents_guide
display_name: Agents Guide
icon: "hub"
description: Guidance for delegating focused work to child Agents.
tools:
  - agents
tags: []
---
AGENTS:
- Delegate automatically when two or more substantial workstreams can proceed independently, a bounded specialist review/research task protects parent context, comparison needs independent evidence, a long goal benefits from implementation/review/test roles, or write work can be isolated safely in Worktrees.
- Also use Agents when the user explicitly asks you to delegate, parallelize, spawn helpers, review independently, research separately, test separately, or keep exploratory work out of the parent thread.
- Do not delegate trivial or ordinary short questions, simple single-step edits, inherently sequential work, work whose delegation overhead exceeds its value, or requests where the user says not to delegate.
- Automatic delegation is unavailable when the Agents tool is disabled or the active profile/tool policy forbids it. Do not try to work around that boundary.
- Prefer focused profiles such as `research`, `plan`, `write`, `review`, or `develop` when they fit the task.
- Use advanced/internal profiles such as `worker`, `synthesize`, or `verify` only for scoped orchestration, synthesis, implementation, or verification work, and respect the active approval/workspace policy.

DELEGATING:
- Call `delegate_work` with a precise objective, a focused context packet, and a profile when useful.
- Give the child enough context to succeed without leaking the full parent transcript by default.
- Use the smallest useful wave and avoid duplicate objectives. Ordinary automatic children are required: call `delegate_work(wait=false, required=true)`. Row-Bot suspends the parent at the generation boundary and automatically produces one consolidated answer after the required barrier.
- `delegate_work(wait=false)` therefore defaults to required automatic orchestration unless you explicitly set `required=false`; the parent thread stays responsive while the durable barrier waits.
- Use `delegate_work(wait=false, required=false)` only for explicitly requested background/fire-and-forget work. Optional work retains its independent completion update and does not block the parent.
- Do not narrate each required child completion or manually poll it. Row-Bot's durable continuation supplies the ordered results when the barrier completes.
- Use `wait=true` only when the user explicitly asks you to wait for the child before answering, or when same-turn synthesis is truly required and cannot be deferred to a follow-up.
- Each automatic child inherits the parent model. Leave `model` empty for automatic work; explicit user-requested child model overrides remain supported.
- Use `delegate_work(use_worktree=true)` only when the user asks for an isolated Worktree or when profile policy requires it for file-editing work. This creates a local git Worktree on its own branch and does not push, fetch, or send messages.
- For natural child-agent model requests like "use gpt5.5 via codex" or "use qwen 3.6 27 B via ollama", the parent agent must reason before delegation: inspect the complete pinned Brain choices with row_bot_status category='model', select the closest active pinned choice, and pass its canonical ref to `delegate_work(model=...)`. Leave `model` empty when the child should inherit the parent model. Do not pass raw natural phrases or unpinned provider refs.
- The `/agent` command is a direct command shortcut, not a natural-language planner. When a user explicitly types `/agent --model=model:provider:model-id ...`, the model value must be a strict active pinned Brain ref or exact pinned label.
- Child Agents cannot change their own runtime model with row_bot_update_setting. If the user wanted a child to use a different model, the parent must spawn it with `delegate_work(model=...)` or the user must use `/agent --model=...`.
- For artifact requests such as "use an agent to write/save/export a file", make the child Agent create the artifact when its profile has the needed write tool. Do not ask the child to return raw content for the parent to export unless the user explicitly wants parent-side synthesis or packaging.
- A review, critique, or edit Agent must receive the complete material it is reviewing: include the complete draft in `context`, or include a concrete workspace-relative artifact path plus the review criteria. Never launch a reviewer against a draft that does not exist yet.
- When the parent will draft the material, finish the draft before delegating the reviewer. When a child drafts it, use a real later wave: let the drafting child finish, then delegate the reviewer from the resumed parent with the returned draft or stable artifact path in `context`.
- `depends_on` controls launch order only; it does not transfer another child's output or inject review material. The parent must consume the review result before finalizing or saving the reviewed artifact.
- Do not give child Agents recursive delegation access unless the selected profile explicitly allows it.

TRACKING:
- Use `agent_status` to inspect running, waiting, stopped, failed, or completed child Agents.
- Use `agent_wait` when the user explicitly asks for the child result or when a later parent turn genuinely needs the child result before answering.
- Use `agent_status(orchestration_id=...)` to inspect a group, including its required barrier and attempts.
- Use `agent_message` to queue parent guidance for one child or an orchestration. Active provider calls are not interrupted; guidance is applied at the next safe boundary.
- Use `agent_stop` for one child or a complete orchestration when work is obsolete, stuck, or the user asks to stop it.
- Use `agent_retry` for a terminal child when a user-requested replacement is appropriate. Row-Bot itself retries only classified transient failures and at most once.
- Summarize child results for the user; do not dump long raw logs unless asked.

PROFILES:
- Use `agent_profiles` to discover available built-in and user Agent Profiles.
- Agent Profiles may narrow inherited tools and pin profile-specific skills. Selected `allow_tools` are the hard runtime tool boundary; profiles with no allow-list inherit all globally enabled tools. Respect `allow_tools`, `skills`, context mode, workspace mode, and approval caps.
- Generic direct Agent requests use the `worker` profile. Select a specialized profile only when the user explicitly names it, such as "use a review agent to..." or `/agent review ...`, or when you are calling `delegate_work` and can justify the profile in your visible handoff. Old folded names such as `quality_reviewer` are accepted as aliases, but canonical slugs are preferred.
- Use `agent_profile_save` only after the user explicitly asks to create or update a reusable Agent Profile. This action is approval-gated.
- Use `agent_promote` only when the user explicitly wants to turn a completed run into a reusable profile or workflow. Workflow promotion creates a disabled manual workflow for review before enabling or scheduling.

HANDOFF:
- When synthesizing child results, state which Agents ran, their status, key evidence, conflicts or uncertainty, and the next action.
