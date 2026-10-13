# local-ai-mcp

**Let Claude Code hand work to a model running on your own machine.**

Claude Code is great at planning and judgement, but every file it reads costs tokens. `local-ai-mcp` is a small MCP server that gives Claude a cheap, private worker: a model running in [Ollama](https://ollama.com). Claude writes the brief and checks the result. The local model does the reading, summarizing and editing. Those tokens cost nothing, and that work never leaves your machine.

- 🧩 **One Python file, standard library only.** No `pip install`, no build step.
- 🔒 **Isolated.** The local agent runs with its own Claude Code config. It can never use your Anthropic account, it can't run Bash, and it can only edit files when you allow it.
- 📊 **Live dashboard.** Watch the output stream in, stop runaway calls, see token stats per session and for all time, and manage your models.
- 🎛️ **Orchestrator mode.** An optional `/local-ai` command that makes Claude delegate *all* hands-on work.

```
 you ──► Claude Code (your normal model and login)
            │  MCP (stdio)
            ▼
         server.py ──► local_ask ─────────► Ollama /api/chat
            │
            ├──────► local_delegate ──► claude -p  (CLAUDE_CONFIG_DIR=~/.claude-local)
            │                               └──► Ollama's Anthropic-compatible API
            ▼
         stats.db (SQLite) ◄── dashboard  http://127.0.0.1:8765
```

> **New to this?** [TUTORIAL.md](TUTORIAL.md) walks you from zero to your first delegated task in about 20 minutes.

---

## Contents

- [Tools](#tools)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Dashboard](#dashboard)
- [Orchestrator mode](#orchestrator-mode-local-ai)
- [Choosing models](#choosing-models)
- [How it works](#how-it-works)
- [Security and privacy](#security-and-privacy)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)
- [Contributing](#contributing)
- [License](#license)

## Tools

| Tool | What it does |
|---|---|
| `local_ask` | One-shot prompt to a local model: summarize logs, classify, reword, draft commit messages. No file access. |
| `local_delegate` | Runs a full Claude Code agent *backed by the local model* in a folder. It can Read/Grep/Glob; Edit/Write only with `allow_edits`. Returns its final answer. |
| `local_extras` | Switches MCP servers or skills on for the local agent, for the rest of the session. Off by default. |

<details>
<summary>Parameters</summary>

**`local_ask`**

| Param | Type | |
|---|---|---|
| `prompt` | string | required |
| `system` | string | optional system prompt |
| `model` | string | alias from `models.json`; default is the first entry |
| `think` | bool | let the model reason first. Slower, better for logic. Default `false` |

Put big shared context first and the question last. Ollama caches the repeated prefix, so follow-up questions come back fast.

**`local_delegate`**

| Param | Type | |
|---|---|---|
| `task` | string | required, the brief |
| `cwd` | string | required, absolute path to work in |
| `allow_edits` | bool | allow Edit/Write. Default `false` (read-only) |
| `model` | string | alias; default is the first entry |

Times out after 30 minutes.

**`local_extras`**

| Param | Type | |
|---|---|---|
| `names` | string[] | names from `extras.json`; `[]` switches everything off |

</details>

## Requirements

| | |
|---|---|
| **macOS on Apple Silicon** (recommended) or **Linux**, 32 GB+ RAM for ~30B models | Windows isn't supported (see [Limitations](#limitations)) |
| **[Ollama](https://ollama.com/download) ≥ 0.14** | runs the models and provides the Anthropic-compatible API |
| **[Claude Code](https://claude.com/claude-code)** (`claude` on your PATH) | your main session, and the engine behind `local_delegate` |
| **Python 3.9+** | already installed on macOS |

## Quick start

```bash
# 1. Get the code
git clone https://github.com/FynnHer/local-ai-mcp.git ~/local-ai-mcp
cd ~/local-ai-mcp

# 2. Pull a model that supports tool calling, and give it a 64k context
#    (Ollama's default context is too small for Claude Code's system prompt)
ollama pull qwen3-coder:30b
printf 'FROM qwen3-coder:30b\nPARAMETER num_ctx 65536\n' > /tmp/Modelfile
ollama create qwen3-coder:30b-64k -f /tmp/Modelfile

# 3. A separate Claude Code config that talks to Ollama, not Anthropic
mkdir -p ~/.claude-local
cat > ~/.claude-local/settings.json <<'EOF'
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:11434",
    "ANTHROPIC_AUTH_TOKEN": "ollama",
    "ANTHROPIC_API_KEY": "",
    "API_TIMEOUT_MS": "1800000",
    "CLAUDE_CODE_ATTRIBUTION_HEADER": "0"
  },
  "model": "qwen3-coder:30b-64k"
}
EOF

# 4. Register your models (the first entry is the default). Edit it if you pulled a different model
cp models.example.json models.json

# 5. Add the MCP server to Claude Code and test it
claude mcp add --scope user local-ai -- python3 ~/local-ai-mcp/server.py
python3 server.py selftest        # → "selftest passed"
```

Restart Claude Code and try:

> Use local_ask to turn `git log --oneline -20` into a short changelog.

> Use local_delegate in this repo: where is configuration loaded? Reply with a path:line list.

The dashboard opens automatically on the first call.

## Configuration

### `models.json`: model registry

Your own file, not tracked by git. Start from `models.example.json`.

```json
{
  "coding":  { "model": "qwen3-coder:30b-64k", "note": "fast, good at code, 64k context" },
  "general": { "model": "qwen3:32b-128k",      "note": "slower, 128k context, for big inputs" }
}
```

- The **first entry is the default**.
- Claude sees the `note`s in the tool description and uses them to pick a model, so say what each model is good at.
- You can also edit this list in the dashboard. Restart Claude Code afterwards so it reads the new tool list.

### `extras.json`: optional tools for the local agent

Optional, not tracked by git. Start with `cp extras.example.json extras.json`.

```json
{
  "mcp": {
    "chrome": { "command": "npx", "args": ["-y", "chrome-devtools-mcp@latest"], "note": "browser control and testing" }
  },
  "skills": {
    "my-skill": "~/.claude/skills/my-skill"
  }
}
```

- `mcp` entries use the same format as Claude Code's `mcpServers`. Claude is shown the `note`.
- `skills` maps a name to a folder that contains a `SKILL.md`. Globs are allowed, and the newest match wins, so plugin cache paths keep working after updates.
- Nothing here is loaded until Claude calls `local_extras` (or you run `/local-ai <name>`). Each extra makes the local model's prompt bigger on every turn.

### Environment variables

| Variable | Default | |
|---|---|---|
| `OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint |
| `LOCAL_CLAUDE_CONFIG` | `~/.claude-local` | config folder for the local Claude Code |
| `DASHBOARD_PORT` | `8765` | dashboard port |
| `LOCAL_AI_OPEN` | (unset) | `0` = don't open the dashboard automatically |

To pass them, add `-e` to the registration: `claude mcp add --scope user local-ai -e DASHBOARD_PORT=8766 -- python3 ~/local-ai-mcp/server.py`

## Dashboard

`python3 server.py dashboard` → http://127.0.0.1:8765 (it also starts by itself on the first tool call)

- **Status**: whether Ollama is up, models in memory, running and queued calls, GPU load.
- **This session / All time**: tokens (input, cached, output), calls, speed, run time, errors. **reset session** starts a new session window; no history is deleted.
- **Live**: one card per running call, with the prompt, streamed output, current phase and a **■ Stop** button. A card turns red after a minute with nothing visible, and the message says whether the model is probably looping (GPU busy) or waiting on something (GPU idle).
- **Recent calls**: click a row to see the output. The prompt is collapsed above it, and **collapse all** closes every open row.
- **Usage by model**: totals per model and tool.
- **Models**: add aliases for installed Ollama models, set the default, and load or unload models from memory.

## Orchestrator mode (`/local-ai`)

[`ORCHESTRATOR.md`](ORCHESTRATOR.md) is a strict set of rules: Claude only plans, writes briefs and verifies, and sends every read and edit to the local model. The bundled skill turns it on with a slash command:

```bash
mkdir -p ~/.claude/skills
cp -r ~/local-ai-mcp/skill/local-ai ~/.claude/skills/
sed -i '' "s#/path/to/local-ai-mcp#$HOME/local-ai-mcp#" ~/.claude/skills/local-ai/SKILL.md   # Linux: sed -i "…"
```

| Command | |
|---|---|
| `/local-ai` | orchestrator mode on |
| `/local-ai chrome` | on, with the `chrome` extra loaded for the local agent |
| `/local-ai off` | back to normal |

Orchestrator mode is optional. Without it, Claude calls the tools when it judges they'll help, or when you ask it to.

## Choosing models

A model works well here if it has:

1. **Tool calling.** Look for the *tools* tag on ollama.com. `local_delegate` needs it.
2. **64k context (32k at the very least).** Claude Code's own prompt takes several thousand tokens, and auto-compact starts summarizing at about half the window. At 32k that leaves little room for reading files. Set `num_ctx` with a Modelfile, as in the quick start; auto-compact depends on it.
3. **Room in RAM.** The weights plus the context cache have to fit; keep the weights under ~60–70% of your RAM.

| RAM | Starting point |
|---|---|
| 16 GB | 7–8B coder model (Q4), 32k context |
| 32 GB | ~30B mixture-of-experts coder (e.g. `qwen3-coder:30b`), 64k context |
| 64 GB+ | larger dense models, or 128k+ context |

Mixture-of-experts models are a good fit: they're big enough to follow a brief and fast enough (dozens of tokens per second) that delegating doesn't feel slow.

Benchmark a model the way the MCP uses it:

```bash
python3 bench.py coding              # alias or raw Ollama model name
python3 bench.py coding --ask-only   # skip the slower delegate scenarios
```

## How it works

- **`local_ask`** streams from Ollama's `/api/chat`, with a 16k-token cap on output to stop runaway generations.
- **`local_delegate`** starts `claude -p` with `CLAUDE_CONFIG_DIR=~/.claude-local`, with every `ANTHROPIC_*`, `CLAUDE*` and `OTEL_*` variable removed from the environment. The child sees Ollama and nothing else. It only gets the tools it needs, and no MCP servers or skills unless you opt in, because every tool definition is prompt the local model re-reads each turn. The server parses its `stream-json` output to show live progress.
- **Auto-compact before overflow.** Claude Code assumes a huge context window for models it doesn't know, so its auto-compact would only kick in after Ollama had already overflowed. `local_delegate` reads each model's `num_ctx` from Ollama and passes it in (`CLAUDE_CODE_MAX_CONTEXT_TOKENS` via `--settings`, which beats `~/.claude-local`). Claude Code then summarizes the conversation once it's about half full. File reads are capped at 1/8 of the window, so a single read can't jump past the threshold. A compaction shows up on the live card as `── context compacted at N tokens ──`. This needs `num_ctx` in the Modelfile; without it, the live card warns you.
- **One model call at a time.** Ollama runs requests one after another anyway. A file lock (`.ollama.lock`) makes the waiting visible: calls from other Claude sessions show up as *queued* and can be stopped.
- **One call per Claude session.** Claude Code moves slow tool calls to the background, so a second call while one is running gets `REFUSED`. Without this, Claude tends to inspect half-written files or send a duplicate brief.
- **Everything is logged** to `stats.db`, which is how the dashboard (a separate process) follows along and sends cancel requests.

## Security and privacy

- Your prompts and code go only to your local Ollama. The local agent can't reach Anthropic: its environment is stripped and its config points at `localhost`.
- The local agent never gets Bash. Without `allow_edits` it can't write anything.
- The dashboard only listens on `127.0.0.1` and only accepts `application/json` POSTs. Other websites can't send that without a CORS preflight, which the server never answers, so they can't trigger actions.
- **`stats.db` contains every prompt and output.** It's in `.gitignore`; keep it out of anything you share.
- Calls from your *main* Claude session still go to Anthropic as usual. This tool cuts how much they read; it doesn't replace them.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Tools don't appear in Claude Code | `claude mcp list` should show `local-ai ✔ Connected`. Re-register, then restart Claude Code |
| `no models registered` | Fill in `models.json` or add a model in the dashboard |
| `… is not installed in Ollama` | The name must match `ollama list` exactly, tag included |
| `local claude failed (exit 1)` | Run `CLAUDE_CONFIG_DIR=~/.claude-local claude -p "hi" --model <model>` yourself and read the error (usually the base URL, or an Ollama that's too old) |
| The agent forgets the task or stops partway, or Ollama reports a context overflow | Create a variant with `num_ctx` in its Modelfile (quick start step 2) so auto-compact knows the real window |
| `Autocompact is thrashing` | The task needs more context than the model has, even after compacting. Split the brief into smaller steps, or use a model with a bigger `num_ctx` |
| A live card is red and the GPU is busy | The model is looping inside a tool call. Stop it and send a narrower brief |
| `REFUSED: … still running` | Working as intended. Wait for the first call's result |
| `can't bind port 8765` | It's already running (`lsof -i :8765`), or set `DASHBOARD_PORT` |
| Reset all stats | Stop the dashboard and Claude sessions, then delete `stats.db` |

## Limitations

- The GPU reading on the dashboard (used to spot a looping model) is macOS-only, because it uses `ioreg`. On Linux it stays empty; everything else works.
- Local models are slower and much weaker at judgement than Claude. Give them narrow, explicit briefs and check what they produce.
- Calls are serialized across all sessions.
- Windows isn't supported (the server uses `fcntl`).

## Contributing

Issues and PRs are welcome. The project is deliberately small: **standard library only, one server file.** Before sending a change, run:

```bash
python3 server.py selftest
```

## License

[MIT](LICENSE)

## Files

| File | |
|---|---|
| `server.py` | MCP server, dashboard server, selftest |
| `dashboard.html` | dashboard UI |
| `bench.py` | model benchmark |
| `models.example.json` | example model registry, copy to `models.json` |
| `extras.example.json` | example MCP servers and skills for the local agent, copy to `extras.json` |
| `LICENSE` | MIT |
| `ORCHESTRATOR.md` | rules for orchestrator mode |
| `skill/local-ai/` | the `/local-ai` slash command |
| `TUTORIAL.md` | step-by-step guide |
