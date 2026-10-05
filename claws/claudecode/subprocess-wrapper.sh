#!/bin/sh
set -eu

# Claude Code 2.1.236 forces permission mode `default` whenever its native
# CLAUDE_CODE_SUBPROCESS_ENV_SCRUB switch is enabled. Keep Auto mode usable
# while preserving the credential boundary: every Bash command, hook, and
# stdio MCP launch enters through this root-owned wrapper.

if [ "$#" -lt 1 ]; then
  printf 'redclaw Claude subprocess wrapper: missing command\n' >&2
  exit 64
fi

command_text=$1
shift

unset CLAUDE_CODE_OAUTH_TOKEN
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
unset AWS_BEARER_TOKEN_BEDROCK
unset GOOGLE_API_KEY GOOGLE_APPLICATION_CREDENTIALS
unset OPENAI_API_KEY OPENROUTER_API_KEY

export HOME=/tmp/redclaw-claude-tool-home
export USER=nobody
export LOGNAME=nobody

# The child runs under a different uid from the root-owned Claude parent, so it
# cannot recover the parent's OAuth token through /proc/<pid>/environ.
exec /usr/bin/setpriv \
  --reuid=nobody \
  --regid=nogroup \
  --clear-groups \
  --no-new-privs \
  /bin/bash -c "$command_text" -- "$@"
