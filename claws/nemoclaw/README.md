# NemoClaw controllers

RedClaw uses NemoClaw v0.0.109 and OpenShell 0.0.101 without native-source
patches. Each evaluation runs in an official OpenShell-managed OpenClaw sandbox.
The matching official v0.0.109 sandbox base image is resolved and validated by
NemoClaw, avoiding a rebuild against package versions that may have disappeared.

## AWS Bedrock

Set `AWS_BEARER_TOKEN_BEDROCK` in the repository's `.env`. Optionally set
`AWS_REGION` (default: `us-east-1`), then run from the repository root:

```bash
python3 -m redclaw redteam \
  --claw nemoclaw --model-provider bedrock --model claude-sonnet-4-5 \
  --dataset /path/to/dataset/trigger --trigger-surface agents.md
```

Use `--model claude-sonnet-4-6` for Sonnet 4.6. The shared Bedrock relay discovers
model IDs, inference profiles, and API support from AWS. It prefers Runtime
InvokeModel, then Converse, then Mantle Messages. Inference errors never cause a
silent switch of model or provider.

The path is:

```text
OpenClaw sandbox -> inference.local -> official OpenShell custom provider
  -> authenticated RedClaw relay in controller -> AWS
```

NemoClaw's official `custom` provider maps `http://127.0.0.1:8000/v1` to the
OpenShell host bridge. AWS credentials stay in the controller's relay process;
NemoClaw registers a separate local relay credential, and the sandbox keeps its
managed `inference.local` endpoint. No agent configuration is patched directly.

The pinned NemoClaw release also has an official Bedrock adapter, but its
Converse conversion splits simultaneous tool results into separate user turns.
AWS then rejects the missing results from the first turn. RedClaw's existing
relay groups those results correctly, so this integration uses the official
custom-provider contract rather than changing NemoClaw's implementation.

AWS gets a separate `redclaw-nemoclaw-bedrock-controller`, registry volumes,
`.runtime/nemoclaw-bedrock` state, and gateway on host loopback port `8082`.
The relay is published on host loopback port `8000`. This avoids replacing the
provider endpoint recorded by existing OpenRouter/custom sandboxes in the normal
controller (gateway port `8080`). Initialization builds and starts the required
service automatically.

Relay assets live under `/opt/redclaw-bedrock`, separate from `/opt/nemoclaw`.
Its supervisor serializes initialization with a file lock, checks authenticated
health, reuses a matching process, and preserves its credential when restarting.
It refuses to replace a different model/credential/code version while OpenShell
sandboxes are running. Parallel cases using the same configuration share a
threaded relay and cached AWS discovery. Run different AWS models sequentially,
since each official gateway has a shared inference route.
