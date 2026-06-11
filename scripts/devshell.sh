#!/usr/bin/env bash
# Land an interactive shell inside the project devcontainer, in the directory
# this script was launched from. The devcontainer mirrors host paths (see
# .devcontainer/devcontainer.json), so the caller's cwd is valid in-container
# and the workspace .venv is the same environment on both sides.
#
# Used as the terminal shell by Zed (.zed/settings.json); also works from any
# plain host shell. Requires the devcontainer CLI on the host:
#   npm install -g @devcontainers/cli
set -euo pipefail

CALLER_DIR=$PWD
TOPLEVEL=$(git rev-parse --show-toplevel)
cd "$TOPLEVEL"

# Rootless podman is the expected engine; fall back to docker.
DOCKER_PATH=$(command -v podman || command -v docker || true)
if [ -z "$DOCKER_PATH" ]; then
    echo "devshell: neither podman nor docker found on PATH" >&2
    exit 1
fi

# The devcontainer CLI is a user-local npm install, which minimal shells (such
# as the one Zed spawns this script from) may not have on PATH. Repair PATH
# rather than hardcoding the shim path: the shim's `#!/usr/bin/env node`
# shebang needs node (also user-local) resolvable on PATH too.
if ! command -v devcontainer > /dev/null; then
    PATH="$HOME/.local/bin:$PATH"
fi
DEVCONTAINER=$(command -v devcontainer || true)
if [ -z "$DEVCONTAINER" ]; then
    echo "devshell: devcontainer CLI not found (npm install -g @devcontainers/cli)" >&2
    exit 1
fi

# Idempotent: builds/starts the container if needed, fast no-op if it is
# already running (~1s of Node CLI startup per terminal — expected).
# stdin is redirected so the Node CLI cannot slurp input meant for the
# interactive shell below (matters when input is piped into this script).
"$DEVCONTAINER" up --docker-path "$DOCKER_PATH" --workspace-folder . > /dev/null < /dev/null

# Exec an interactive shell in the caller's directory with the workspace .venv
# active. The outer non-login `bash -c` skips the profile, and activation
# happens immediately before `exec bash -i`, so no startup file runs between
# activation and the shell that could clobber the venv PATH entry. Paths are
# passed as positional parameters so spaces survive. If the caller's dir is
# not visible in-container, fall back to the workspace root so venv
# activation still happens.
exec "$DEVCONTAINER" exec --docker-path "$DOCKER_PATH" --workspace-folder . \
    bash -c 'cd "$1" || cd "$2"; if [ -f "$2/.venv/bin/activate" ]; then . "$2/.venv/bin/activate"; fi; exec bash -i' \
    devshell "$CALLER_DIR" "$TOPLEVEL"
