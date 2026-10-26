# Swarmify

A swarm of Claude Code agents sharing one graphify memory per world, running gated workflows. Python backend (FastAPI, asyncio, SQLite, files), React web skin, one monorepo. Domain-driven: one package per bounded context, contexts share only ids and events.

## Agent skills
- Domain docs, specs, ADRs, glossary and research live in the Obsidian vault, not here. See `CONTEXT.md` for the pointer.
- Work items are GitHub issues. Conventions: `docs/agents/issue-tracker.md`.
- Method: aha skills (grill, spec, split, build, dig, handoff). Ponytail governs code size; every non-trivial unit ships with its test.
- No em-dashes anywhere: code, comments, commits, docs.
