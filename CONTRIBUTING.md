# Contributing

Bug reports, feature requests, and pull requests are welcome on [GitHub](https://github.com/saemeon/mypackage/issues).

## Development setup

```bash
git clone https://github.com/saemeon/mypackage
cd mypackage
uv sync --group dev
```

Pre-commit hooks are managed with [prek](https://github.com/saemeon/prek). They run automatically on `git commit` once you have installed the dev dependencies.

## Running checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```

## Building docs

```bash
uv sync --group doc
uv run mkdocs serve
```
