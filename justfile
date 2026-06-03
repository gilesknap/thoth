# thoth recipes.

# The server must be reachable at the given port on the loopback (e.g. via an SSH
# local-forward to the appliance). The key is read at the prompt, never stored in
# shell history.
#
# Register thoth's HTTP MCP server with Claude Code (prompts for the bearer key).
thoth-mcp name="thoth-vps" port="8765" scope="user":
    #!/usr/bin/env bash
    set -euo pipefail
    url="http://127.0.0.1:{{ port }}/mcp"
    read -sp "thoth MCP bearer key: " key && echo
    [ -n "$key" ] || { echo "no key entered; aborting" >&2; exit 1; }
    claude mcp remove -s {{ scope }} {{ name }} >/dev/null 2>&1 || true
    claude mcp add --transport http {{ name }} "$url" \
        -s {{ scope }} --header "Authorization: Bearer $key"
    unset key
    echo "Added MCP server '{{ name }}' -> $url (scope {{ scope }}). Restart Claude Code (or run /mcp) to pick it up."
