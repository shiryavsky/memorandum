# Security Policy

## Reporting a Vulnerability

If you find a security issue in Memorandum, **please do not open a public
issue.** Send the details privately to the maintainers via [GitHub Security
Advisories](https://github.com/shiryavsky/memorandum/security/advisories/new)
(preferred) so we can coordinate a fix before disclosure.

Memorandum is a personal aggregator that handles credentials for chat
platforms and email; the most sensitive surfaces to report on are:

- **Credential leaks** — anything that could expose tokens stored in
  `config.yaml` (logs, error messages, the `tool_calls` audit table, etc.).
- **`send_message` abuse paths** — bugs that let the agent send to a source
  whose `allow_send: false` is set, or that bypass the read-before-send guard.
- **`config.yaml` write paths** — the three MCP alias-write tools mutate the
  config file via `ruamel.yaml`; any path that lets them target a key outside
  the documented schema or escape the soft caps.
- **MCP arguments leakage** — the `tool_calls.args_summary` redaction map is
  load-bearing for not leaking `send_message` body text into the dashboard.
  A regression there is sensitive.

We aim to acknowledge reports within 7 days and ship a fix or coordinated
disclosure within 30. There is no bug bounty.

## Scope

The project is a single-operator tool — it is not designed for multi-tenant
deployment. Threat models that assume an adversarial operator on the same
machine, or that require hardening for hostile networks, are out of scope.
The `data/` directory and `config.yaml` are expected to be operator-private.
