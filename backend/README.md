# voxarium_studio — Voxarium Studio backend

FastAPI + asyncio backend for Voxarium Studio: the pipeline execution engine, plugin
registry, Explorer and Teams orchestrator.

```bash
uv venv .venv --python 3.12
uv pip install -e ".[teams]"
voxarium_studio run /path/to/your/game-project
```

Full documentation lives in the repository root: [`../README.md`](../README.md) and
[`../docs/`](../docs/).
