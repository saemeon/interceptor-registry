[![PyPI](https://img.shields.io/pypi/v/mypackage)](https://pypi.org/project/mypackage/)
[![Python](https://img.shields.io/pypi/pyversions/mypackage)](https://pypi.org/project/mypackage/)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![ty](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ty/main/assets/badge/v0.json)](https://github.com/astral-sh/ty)
[![prek](https://img.shields.io/badge/prek-checked-blue)](https://github.com/saemeon/prek)

# mypackage

A short description of what this package does.

**Full documentation at [saemeon.github.io/mypackage](https://saemeon.github.io/mypackage/)**

## Installation

```bash
pip install mypackage
```

## Quick Start

```python
import mypackage

# example usage
```

# How to Track Template Changes

1. Add the remote
run `git remote add template https://github.com/saemeon/pytemplate.git`

2. Fetch the data
run `git fetch template`

3. Create a local branch that tracks the template's main
run `git checkout -b pytemplate-main template/main`

4. Switch back to your work branch and merge the template in
run `git checkout main`
run `git merge pytemplate-main --allow-unrelated-histories`

## License

MIT
