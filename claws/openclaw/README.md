# openclaw evaluation image

Build from the project root:

```bash
docker build -t redclaw-claw-openclaw:latest claws/openclaw
```

The Dockerfile restores the original evaluation build recipe and fetches the pinned source revision. See [the image guide](../README.md) for version pins and the distinction between rebuilding and replaying an existing image.

For comparison with existing experiments, first run:

```bash
python -m redclaw runtime verify --claw openclaw
```

Use the same frozen dataset, model/provider and run parameters.
