---
name: local-ai
description: Switch this session into local-AI orchestrator mode, where hands-on work is delegated to the local model through the local-ai MCP. Only when the user runs it. Arguments can switch on extras (MCP servers, skills) for the local agent.
disable-model-invocation: true
argument-hint: "[off | extra names, e.g. ui5 ui5-tables]"
---
Arguments: "$ARGUMENTS"

If the arguments are `off`: call `local_extras` with `[]`, then local-AI orchestrator mode is OFF from now on. Stop following the ORCHESTRATOR.md rules and work normally again (you may still call the local-ai tools if the user asks). Confirm in one line.

Otherwise: local-AI orchestrator mode is ON for the rest of this session.
1. Read /path/to/local-ai-mcp/ORCHESTRATOR.md now and follow it strictly until the user runs `/local-ai off`.
2. If the `local_delegate` / `local_ask` / `local_extras` tools aren't available, say so and stop: the local-ai MCP isn't registered in this Claude.
3. If the arguments contain names (anything besides a task), call `local_extras` with exactly those names. Otherwise leave extras off.
4. Confirm in one line ("Local AI mode on: delegating to `coding`, extras: ui5."), then continue with the user's request if one came with this command.
