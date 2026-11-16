# AgentHub Documentation

The [repository README](../README.md) is the English entry point for reviewers and team members. It describes what currently runs, how to start it and how to exercise its API.

| Document | Purpose and status |
| --- | --- |
| [Development handoff](DEVELOPMENT_HANDOFF.md) | Fresh-machine setup, merged baseline, regression guarantees and next work |
| [Agent execution completion plan](EXECUTION_COMPLETION_PLAN.md) | Review of main at `506bc66`, instructor feedback, remaining worker/model work and evaluation criteria |
| [Agent execution contract](AGENT_EXECUTION.md) | Implemented bounded model tools, checkpoints, stale approval checks, migration and SSE/API handoff |
| [Setup and team workflow](../README.md) | Current Docker services, accounts, shared access, API examples, tests and migration workflow |
| [Code structure](CODE_STRUCTURE.md) | A concise map of services, source modules, migrations, tests and the message flow |
| [Technology stack](TECH_STACK.md) | Current languages, libraries, versions, responsibilities and selection rationale |
| [Backend database reference](BACKEND_DATABASE.md) | Current chat and refund check tables, constraints and transaction boundaries |
| [Manual testing guide](MANUAL_TESTING.md) | Chat and refund workflow checks with expected outcomes and a test result record template |
| [Architecture and team handover](AGENTHUB_TEAM_HANDOVER.md) | Original English architecture proposal; planned features are not claims of implementation |
| [Developer documentation](developer/README.md) | Index of Chinese team explanations and historical specification drafts |

The current implementation provides durable chat submission and a simulated refund workflow with human approval, using fixed commands or opt-in OpenAI tool selection. See [Worker baseline](WORKER_BASELINE.md) for setup and [Agent execution](AGENT_EXECUTION.md) for limits and recovery. [Live updates](LIVE_UPDATES.md) stream conversation changes over SSE. Production cloud delivery remains separate integration work.

Reviewer-facing documentation and system-generated runtime text are English. Source comments use English and Chinese. Chinese prose documents belong under `docs/developer/zh-CN/`; user-submitted Unicode content is preserved.
