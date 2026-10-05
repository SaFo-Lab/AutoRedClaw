# Codex CLI adapter image

The bundled Dockerfile installs the pinned Codex CLI used by
`redclaw.adapters.codex`. No credentials are included.

From the project root, evaluate an existing frozen dataset:

```bash
python3 -m redclaw redteam \
  --claw codex --model-provider codex --model gpt-5.6-sol \
  --dataset /path/to/dataset/trigger --trigger-surface agents.md
```

`CodexAdapter.initialize()` builds the image and starts device authentication
when needed. Authentication is retained in a Docker volume; set
`CODEX_AUTH_VOLUME` to choose it. `CODEX_CLI_VERSION` selects a CLI version,
`CODEX_IMAGE` selects an image, and `--force-initialize` forces initialization.
API-key evaluation uses `--model-provider openai` and `OPENAI_API_KEY`.

Each case uses a disposable container. The adapter preserves its existing
approval-review behavior. Native surfaces are `agents.md`, `skill.md`, and
`environment`; unsupported workspace files are rejected.
