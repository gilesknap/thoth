# Contribute to the project

Contributions and issues are most welcome! All issues and pull requests are
handled through [GitHub](https://github.com/gilesknap/thoth/issues). Also, please check for any existing issues before
filing a new one. If you have a great idea but it involves big changes, please
file a ticket before making a pull request! We want to make sure you don't spend
your time coding something that might not fit the scope of the project.

## Issue or Discussion?

Github also offers [discussions](https://github.com/gilesknap/thoth/discussions) as a place to ask questions and share ideas. If
your issue is open ended and it is not obvious when it can be "closed", please
raise it as a discussion instead.

## Code Coverage

While 100% code coverage does not make a library bug-free, it significantly
reduces the number of easily caught bugs! Please make sure coverage remains the
same or is improved by a pull request!

## Developer Information

The development environment is a [devcontainer](https://containers.dev/) built
from the `developer` target of the root `Dockerfile`, intended for rootless
podman (or docker with user namespaces — the container runs as root and the
user-namespace mapping keeps workspace files owned by your host user).

The project virtual environment is a plain uv-managed `.venv` in the workspace
root. The devcontainer bind-mounts the workspace (and a shared cache at
`$HOME/.cache/devcontainer-shared`) at **identical paths inside and outside the
container**, so the one `.venv` is valid from both sides: host editors and
language servers resolve the same interpreter and site-packages that
in-container tooling uses.

### VSCode

Open the repository in [VSCode](https://code.visualstudio.com/docs/devcontainers/containers)
and "Reopen in Container". The post-create step creates `.venv` with `uv sync`
and installs the pre-commit hooks.

### Zed

Zed's language servers run on the host and pick up the workspace `.venv`
directly. Project terminals are routed through `scripts/devshell.sh` (see
`.zed/settings.json`), which starts the devcontainer if needed and lands an
interactive shell inside it — in the directory the terminal was opened from,
with `.venv` activated. Host prerequisite:

```bash
npm install -g @devcontainers/cli
```

### Running uv on the host

Export this in your shell profile so host and container agree on where
uv-managed interpreters live (otherwise a host `uv sync` may rebuild `.venv`
against a different interpreter location):

```bash
export UV_PYTHON_INSTALL_DIR=$HOME/.cache/devcontainer-shared/uv-python-installs
```

When syncing on the host, match the post-create step's extras — a bare
`uv sync` would remove the runtime libraries that `src/` imports:

```bash
uv sync --extra runtime
```

### Known quirks

- uv may warn that it failed to hardlink and is copying packages (cache and
  workspace on different filesystems). Harmless.
- Path mirroring is an optimisation for Linux hosts: opening the project at a
  different path (or on another OS) degrades gracefully — the container
  recreates `.venv` for its own path via `uv venv --clear` in the post-create
  step.
- Each devshell terminal takes about a second to spawn (devcontainer CLI
  startup latency).

This project was created using the [Diamond Light Source Copier Template](https://github.com/DiamondLightSource/python-copier-template) for Python projects.

For more information on common tasks like running the tests and setting a pre-commit hook, see the template's [How-to guides](https://diamondlightsource.github.io/python-copier-template/5.1.0/how-to.html).
