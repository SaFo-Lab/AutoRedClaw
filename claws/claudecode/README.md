# Claude Code adapter image

The bundled Dockerfile installs the pinned Claude Code CLI used by
`redclaw.adapters.claudecode`. No credentials are included.

From the project root, evaluate an existing frozen dataset:

```bash
python3 -m redclaw redteam \
  --claw claudecode --model-provider claudecode \
  --model claude-sonnet-4-5-20250929 \
  --dataset /path/to/dataset/trigger --trigger-surface agents.md
```

`ClaudeCodeAdapter.initialize()` builds the image and starts the native
`setup-token` flow when needed. `CLAUDE_CODE_AUTH_VOLUME` selects the Docker
volume that retains authentication. The adapter sends the token to the parent
Claude process over stdin. The included subprocess wrapper removes provider
credentials and drops privileges for tool subprocesses.

`CLAUDECODE_PERMISSION_MODE` controls the existing native permission behavior;
the default is `dontAsk`. The adapter retains native tools and records the
requested/effective permission mode. Native surfaces are `agents.md` (mapped to
`CLAUDE.md`), `skill.md`, and `environment`; unsupported workspace files are rejected.
