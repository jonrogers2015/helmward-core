# Helmward Setup Guide

Welcome! This guide walks you through setting up Helmward from a fresh clone to a fully working system with your first agent registered and running a task.

**Time to complete:** 15–30 minutes.

---

## Prerequisites

Before you start, make sure you have:

- **A Linux host** — a VM, LXC container, or bare metal running Debian 13 (or a
  recent Debian/Ubuntu derivative), with root access (either via `sudo`, or by
  being logged in as root directly — a minimal container image may not have
  the `sudo` binary installed at all, and that's fine either way). This is
  the recommended and tested setup.
- **Git** — to clone this repo. Not preinstalled on every minimal image;
  if `git --version` comes back empty, `apt-get install git` first.
- **Claude Desktop** or **Claude Code CLI** — for orchestrating your agents via MCP
- **Node.js 18+** — only needed for remote MCP access via `npx mcp-remote`
  (Step 3, Option B), or if you plan to run agent-side scripts locally

Everything else — Python, the virtualenv — is installed for you by the install
script in Step 2.

You do **not** need a domain name, a cloud account, or any paid service to get a
fully working local setup.

---

## Step 1 — Get the files

Clone the repo to a folder of your choice, e.g. `~/helmward`:

```bash
git clone https://github.com/jonrogers2015/helmward-core.git ~/helmward
cd ~/helmward
```

Inside, you'll find:
```
helmward/
├── control-plane/     # The FastAPI + SQLite backend
├── dashboard/          # The web dashboard (served by the control plane)
├── mcp-bridge/          # The MCP server Claude connects through (Step 3)
├── install.sh          # Native installer
├── tools/doctor.py      # Diagnostic script -- run this if something's wrong
└── docs/                # This guide
```

---

## Step 2 — Install and start the control plane

From the folder you just cloned, run the installer:

```bash
sudo ./install.sh
```

By default this installs to `/opt/helmward`. To choose a different location,
pass it as an argument:

```bash
sudo ./install.sh /srv/helmward
```

The installer will:

- install system dependencies (Python 3, venv, curl, sqlite3)
- create a Python virtualenv and install the control plane's requirements
- initialize the database from `schema.sql` — **first run only**, so re-running
  the installer to upgrade will never wipe your existing task history
- install and start the `helmward-control-plane` systemd service
- check `/healthz` and print a clear pass/fail result

On success you'll see:

```
==============================================
 INSTALL PASSED
   healthz : {"ok": true}
   dashboard: http://<this-host>:8080/dashboard.html
   config   : /opt/helmward/helmward.env
==============================================
```

The script is safe to re-run at any time.

**Verify it's running:**
```bash
curl http://127.0.0.1:8080/healthz
```
You should see `{"ok": true}`.

Open the dashboard in your browser: **http://127.0.0.1:8080/dashboard.html**

You should see an empty dashboard — no agents or tasks yet. That's expected;
you'll register your first agent next.

**Managing the service:**
```bash
systemctl status helmward-control-plane      # is it running?
systemctl restart helmward-control-plane     # apply a config change
journalctl -u helmward-control-plane -f      # follow the logs
```

**Configuration** lives in one file — `helmward.env` in your install directory
(e.g. `/opt/helmward/helmward.env`). Edit it, then restart the control plane for
changes to take effect. The installer never overwrites this file on re-run.

**Before going further:** if you plan to send Telegram notifications for
approvals, set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `helmward.env` to
your own bot's credentials (create one via [@BotFather](https://t.me/BotFather)).
These are left blank by default — Helmward runs fine without them, you just won't
get Telegram alerts for pending approvals until they're set.

---

## Step 3 — Connect Claude to Helmward (MCP)

Helmward exposes an MCP server that Claude Desktop or Claude Code can use to create tasks, check on agents, and read your wiki. **We recommend local (stdio) connection as the default** — it's the simplest, most reliable option, with nothing exposed to the internet.

### Option A — Local (recommended for most users)

If you're running Helmward and Claude Desktop on the same machine, add this to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "helmward": {
      "command": "python3",
      "args": ["/path/to/helmward/mcp-bridge/server.py"],
      "env": { "CONTROL_PLANE_URL": "http://127.0.0.1:8080" }
    }
  }
}
```

Fully quit and reopen Claude Desktop afterward for the change to take effect.

### Option B — Remote access via Tailscale Funnel

If you want to reach your Helmward instance from another device, or you're running it on a home server and connecting from elsewhere, use [Tailscale Funnel](https://tailscale.com/kb/1223/funnel) — it's free and gives you a **fixed hostname that never changes**, even across restarts.

1. Install Tailscale on the machine running Helmward: `curl -fsSL https://tailscale.com/install.sh | sh`
2. Authenticate: `tailscale up`
3. Expose the MCP port: `tailscale funnel --bg 8765`
4. Tailscale will print your public hostname, e.g. `https://yourmachine.yourtailnet.ts.net`

Use that hostname in your MCP client config with the `mcp-remote` transport (requires Node.js 18+):
```json
{
  "mcpServers": {
    "helmward": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://yourmachine.yourtailnet.ts.net/mcp"]
    }
  }
}
```

Unlike quick-tunnel services, this hostname is permanent — you'll never need to update this config again due to a URL change.

---

## Step 4 — Register your first agent

An "agent" in Helmward is any worker — local model, cloud model, or scripted process — that can pull tasks and execute them. The simplest way to register one is via a `curl` call:

```bash
curl -X POST http://127.0.0.1:8080/api/work/register \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "my-first-agent",
    "role_name": "My First Agent",
    "capabilities": ["general"]
  }'
```

Refresh the dashboard — you should see `my-first-agent` listed, with status `online`.

---

## Step 5 — Verify end-to-end

From Claude Desktop or Claude Code (with the MCP connection from Step 3 active), ask Claude to create a task:

> "Use Helmward to create a task with the prompt 'say hello' and capability 'general', then check its result."

You should see the task appear in the dashboard, move through `queued` → `running` → `done`, and Claude should report back a result. If it does, your setup is complete.

---

## Step 6 — Switching inference models (optional, for local LLM setups)

**Skip this section if you're only using cloud models (Claude, OpenAI, etc.) as agents.** It's only relevant if you're running local models through [llama-swap](https://github.com/mostlygeek/llama-swap) — a common setup for homelab/self-hosted AI.

If your agents pull inference from a llama-swap instance, Helmward's dashboard includes a **Models** page that lets you see every model registered with llama-swap and switch the active one with a single click — no config file editing required.

### One-time setup

Set two environment variables for the control plane (in `helmward.env` in your
install directory):

```
LLAMA_SWAP_URL=http://<your-inference-host>:8081
HERMES_WEBUI_URL=http://<your-agent-host>:8787
```

Adjust the hosts/ports to match your actual setup — these are the addresses of your llama-swap instance and your agent's web UI (if it exposes a model-switching endpoint), respectively.

### Using the Models page

1. Open the dashboard, click **Models** in the left nav
2. You'll see every model currently registered in llama-swap
3. Click any model to switch to it — Helmward validates the model is actually available in llama-swap first, then switches your agent's active model
4. A note in the panel shows what was last switched *from this dashboard* — it can't detect model changes made another way (a script, or your agent's own settings panel), so treat it as a helpful hint, not a full status readout

---

## Troubleshooting

**Something not working? Run the diagnostic tool first:**
```bash
/opt/helmward/control-plane/venv/bin/python3 tools/doctor.py
```
It checks for common issues directly against your live system rather than
guessing -- including a stray old NATS server left over from an earlier setup
attempt, which can block the control plane from starting at all if a leftover
`Requires=` dependency exists on the systemd unit.

### The dashboard won't load
Confirm the service is running with `systemctl status helmward-control-plane`, and check its logs with `journalctl -u helmward-control-plane -n 50 --no-pager`.

### Claude says "could not attach to MCP server"
- **Local setup:** double-check the file path in your config matches where you cloned Helmward, and that Python 3.11+ is installed and on your PATH.
- **Tailscale Funnel setup:** run `tailscale funnel status` to confirm Funnel is still active and check the hostname matches your config exactly.
- Either way, a full quit-and-reopen of Claude Desktop (not just closing the window) is often required after any config change.

### An agent shows "offline" even though it's running
- Agents must send a heartbeat at least once a minute to show as online. Check that your agent's polling loop is actually running and reaching the control plane.
- If self-hosting the control plane remotely, confirm your firewall allows the agent's outbound connection.

### Tasks stay stuck in "queued"
- No agent with a matching `capability` is currently online. Check the Agents panel in the dashboard.
- If an agent is online but not claiming tasks, confirm its polling loop is actually calling `POST /api/work/claim`.

### A task with a `verification_spec` is immediately rejected with a 400 error
- The `type` field in your `verification_spec` isn't one of the supported values. Valid types are: `file_exists`, `file_checksum`, `command_output_contains`, `command_exit_code`, and **`agent_result_matches_probe`** (the last one checks the agent's own claimed result against an independent raw probe of ground truth — useful for catching a model that confidently reports the wrong thing). Fix the typo and resubmit.

### The Models page says "llama-swap unreachable" or shows no models
- Confirm `LLAMA_SWAP_URL` (in `helmward.env`) points to the right host and port, and that llama-swap is actually running there.

### Switching a model reports "not a registered llama-swap alias"
- The name you gave doesn't match what llama-swap actually calls it. llama-swap aliases are usually short names you chose in its own config (e.g. `my-model-7b`), not the raw `.gguf` filename. Check the Models dashboard page to see the exact registered names.

---

## Quick reference

| What | Command |
|---|---|
| Install / upgrade | `sudo ./install.sh [install-dir]` |
| Start the service | `systemctl start helmward-control-plane` |
| Stop the service | `systemctl stop helmward-control-plane` |
| View logs | `journalctl -u helmward-control-plane -f` |
| Run diagnostics | `venv/bin/python3 tools/doctor.py` |
| Dashboard | http://127.0.0.1:8080/dashboard.html |
| Health check | `curl http://127.0.0.1:8080/healthz` |

---

Questions or issues? Open an issue on the [GitHub repo](https://github.com/jonrogers2015/helmward-core), or reach out through [Apex Solutions](https://github.com/jonrogers2015) if you'd like help with customization, extension, or a managed setup.
