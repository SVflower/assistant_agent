# Security Policy

Assistant Agent is a local-first agent runtime. Its permission layer, workspace
boundary, and optional container mode reduce risk but do not make untrusted
prompts, Skills, MCP servers, or model providers safe by themselves.

`workspace` mode is not an operating-system sandbox. Web runtime, external MCP
servers, and custom Python tools may run outside the container boundary. Review
these boundaries before using production data.

## Reporting

Do not open a public issue for a security vulnerability. Report it privately via
the repository owner's GitHub security contact with the affected commit, safe
reproduction, impact, and proof of concept. Never include real keys, session
files, customer data, or private MCP credentials.

## Operational rules

- Keep `config.yaml`, `.env`, Session/Run state, logs, attachments, and outputs local.
- Use environment variables or a secrets manager for provider and MCP credentials.
- Review third-party Skills and MCP servers before installation.
- Pin production container images and dependencies where reproducibility matters.
- Treat model output and tool arguments as untrusted input.
- Disable network and extensions unless the task requires them.
