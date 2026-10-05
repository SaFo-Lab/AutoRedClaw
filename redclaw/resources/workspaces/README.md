# Native workspace templates

These are runtime fixture data, copied without content changes from the source
revisions listed in `claws/sources.json`:

- ZeroClaw: `crates/zeroclaw-runtime/src/agent/personality_templates/`.

Only the seven workspace files consumed by each adapter are included, alongside
the upstream licenses. The runtime reads these bundled templates directly and
never needs an external source checkout. When changing the ZeroClaw source revision
in its Dockerfile, update the templates to that revision as well.
