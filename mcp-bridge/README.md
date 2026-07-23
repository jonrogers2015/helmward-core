# Helmward MCP Server

Connects Claude.ai directly to your Helmward agent OS.

## Install

```bash
cd /root/helmward-mcp
pip install -r requirements.txt
```

## Add to Claude.ai

Go to Claude.ai Settings → Integrations → Add MCP Server:

```json
{
  "helmward": {
    "command": "python3",
    "args": ["/root/helmward-mcp/server.py"],
    "env": {}
  }
}
```

## Tools available

- create_task — fire a task at Apex or Rook
- get_task — poll task status and result
- list_tasks — see recent tasks
- list_agents — see agent registry and status
- get_wiki_page — read a wiki page
- list_wiki_pages — list all wiki pages
