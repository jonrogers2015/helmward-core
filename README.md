# Helmward

A self-hosted control plane for AI agent orchestration, built around one
idea: **don't trust an agent's claim about what it did -- check it.**

Helmward lets Claude (or any other agent) create tasks, dispatch them to
workers, and -- the part most orchestration tools skip -- independently
verify the result against real system state before marking anything
done. A task can carry a `verification_spec`: a file that should exist, a
command that should exit clean, output that should contain a specific
string, or an agent's own claimed result checked against an independent
probe. No verification spec, no blind trust -- the gate runs, or the task
doesn't get marked done.

This came out of running local models as real agents and watching them
occasionally report success on work that hadn't actually happened. The
verification layer is the fix, and it's the reason this project exists.

## What's here

- **`control-plane/`** -- FastAPI + SQLite backend. Task dispatch, agent
  registration, the verification gate.
- **`dashboard/`** -- the web UI, served by the control plane.
- **`mcp-bridge/`** -- the MCP server Claude connects through to create
  tasks, check agents, and read the wiki.
- **`install.sh`** -- native installer (systemd, no Docker required).
- **`tools/doctor.py`** -- a diagnostic script that checks your live
  system directly rather than guessing.
- **`docs/HELMWARD-SETUP-GUIDE.md`** -- full setup walkthrough, 15-30
  minutes to a working system.

## Quick start

```bash
git clone https://github.com/jonrogers2015/helmward-core.git ~/helmward
cd ~/helmward
sudo ./install.sh
```

Then open `http://127.0.0.1:8080/dashboard.html`. Full walkthrough,
including connecting Claude via MCP and registering your first agent, is
in [`docs/HELMWARD-SETUP-GUIDE.md`](docs/HELMWARD-SETUP-GUIDE.md).

## Verification, not vibes

The core mechanism is deliberately simple and has no model in the check
path: `file_exists`, `file_checksum`, `command_exit_code`,
`command_output_contains`, and `agent_result_matches_probe` (checks an
agent's own claimed result against an independent raw probe of ground
truth). The same verification approach, extracted and generalized, is
also published standalone as [`attested`](https://github.com/jonrogers2015/attested)
on PyPI -- Helmward's control plane depends on it directly rather than
maintaining its own copy.

## License

Apache 2.0. See [`LICENSE`](LICENSE).

## Support

This is an open-source project. Issues and PRs welcome.

If you're self-hosting this and want it customized, extended, or run
as a managed instance, that work happens through **Apex Solutions**
-- verification-first document and data automation consulting, built
on the same principle as the gate above: check the artifact, don't
trust the claim. Reach out through [GitHub](https://github.com/jonrogers2015).
