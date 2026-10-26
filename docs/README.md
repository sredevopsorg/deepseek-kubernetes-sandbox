# Repository documentation

Repository-wide documentation lives here. Example-specific material lives with
its example under [`examples/<name>/docs/`](../examples/) — see the
[agents-as-a-service architecture](../examples/dsh-agents-as-a-service/docs/architecture.md)
for the deep-dive that used to live in this directory.

If you are new, read the [root README](../README.md) first: it is image-first and
gets a DSH Web container running before anything else.

## Index

| Document | Read it for |
| --- | --- |
| [main-image.md](main-image.md) | The primary artifact: build args, runtime environment, entrypoint, probes, volumes/ports/signals, non-root contract, image names, tags and digests. |
| [examples.md](examples.md) | The example contract: required layout, manifest rules, exposure variants, components and image naming, scripts, docs, and how to add a new example. |
| [gitflow.md](gitflow.md) | Branches, protected `main`, squash merges, Conventional PR titles, required CI checks, release branches, hotfixes and the one-time GitHub settings checklist. |
| [versioning.md](versioning.md) | Release identity (`DSH ref` + optional `-rN`), why the current identity is `dsh-v0.2.1-rc.3-r3`, channels, compatibility matrix, bumping pins, the release checklist, rollback, git-cliff and digest pinning. |

## Project skills

DSH discovers project skills from `<git root>/.dsh/skills/<name>/SKILL.md`
(the nearest `.git` ancestor). This repository ships one:

| Skill | Read it for |
| --- | --- |
| [dsh-agent-sandbox-platform](../.dsh/skills/dsh-agent-sandbox-platform/SKILL.md) | Operating and extending the agents-as-a-service platform, agent-sandbox MCP limits and deployment, and a read-only preflight. |

## Example documentation

| Document | Read it for |
| --- | --- |
| [examples/README.md](../examples/README.md) | The index of implementations in this repository. |
| [dsh-agents-as-a-service README](../examples/dsh-agents-as-a-service/README.md) | The multi-tenant platform walkthrough: warm pool, claims, suspend/resume, gateway, exposure and troubleshooting. |
| [dsh-agents-as-a-service architecture](../examples/dsh-agents-as-a-service/docs/architecture.md) | Container/sequence diagrams, ADRs, security and RBAC, failure modes and the evolution path. |
| [gateway README](../examples/dsh-agents-as-a-service/gateway/README.md) | The gateway's HTTP/WebSocket API, configuration, auth (including SSO), and limitations. |

## Project policies

| Document | Read it for |
| --- | --- |
| [AGENTS.md](../AGENTS.md) | Repository conventions for AI agents and humans, and the commands the CI runs. |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | Local setup, the change workflow and what a pull request needs. |
| [SECURITY.md](../SECURITY.md) | Vulnerability reporting and project-specific threat notes (pod logs, SSO header trust, shared provider key, TLS). |
| [CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md) | Community standards. |
| [CHANGELOG.md](../CHANGELOG.md) | Generated release history (do not edit by hand). |
| [LICENSE](../LICENSE) | MIT. |
