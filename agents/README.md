# Agent definitions

Drop-in agent definitions for people who drive boxman with an AI coding agent.
They are **for you, the boxman user or contributor** — nothing in the boxman
CLI reads this directory, and the project's own tooling does not load it.

| File | Audience | Covers |
|---|---|---|
| `boxman-user.md` | Operating boxman | The `conf.yml` schema, the CLI, libvirt resource naming, the networking model, templates and clone identity, ISO/PXE boot, OCI images, container clusters, snapshots and storage reclaim, and the failure modes that look like bugs but are not |
| `boxman-developer.md` | Working on boxman | Repository layout, the runtime/provider abstractions, the manager mixins, the clone identity pass, error, sudo and destructive-operation conventions, build and test workflow (including the disposable test-runner VM), and the CI gate |

## Installing

Both files carry YAML frontmatter with a `name` and a `description`, which is
the format Claude Code expects for a subagent. Copy or symlink them into an
agents directory:

```bash
# available in every project
mkdir -p ~/.claude/agents && cp agents/boxman-*.md ~/.claude/agents/

# or scoped to one checkout
mkdir -p .claude/agents && cp agents/boxman-*.md .claude/agents/
```

To use them as skills instead, put each file at
`<skills-dir>/<name>/SKILL.md` and rename the frontmatter's `tools:` key to
`allowed-tools:`.

Other agent harnesses generally accept the same shape: the body is plain
Markdown, and the frontmatter carries the name plus the description used to
decide when the agent applies.

## Keeping them accurate

They describe boxman as of the commit they ship with. If you change the CLI
surface, the config schema, or user-visible behaviour, update the matching
sections here in the same change — and `boxman-developer.md` too when the change
moves the architecture, a convention, or the build/test workflow. A stale agent
definition is worse than none, because it produces confident wrong answers.
