# Tutorial: your first local-AI session

At the end of this tutorial, Claude Code will hand reading and editing work to a model running on your Mac, and you'll watch it happen live in the dashboard. Allow about 20 minutes, plus the model download (~19 GB).

For reference material (every option and file), see [README.md](README.md).

---

## 0. Check what you have

Open Terminal:

```bash
uname -m              # should print arm64 (Apple Silicon)
sysctl -n hw.memsize | awk '{print $1/1e9 " GB RAM"}'
python3 --version     # 3.9 or newer
claude --version      # Claude Code installed and logged in
```

No Claude Code yet? Install it from https://claude.com/claude-code and run `claude` once to log in.

**16 GB of RAM?** Everything below still works. In step 2, pick a smaller model (for example a 7–8B coder model) and use `32768` instead of `65536` for the context.

## 1. Install Ollama

Download it from https://ollama.com/download and start the app (a llama icon appears in the menu bar). Check it:

```bash
ollama --version                  # 0.14 or newer
curl -s localhost:11434           # "Ollama is running"
```

## 2. Get a model with a big context window

Claude Code sends a long system prompt, so the model needs a large context. Ollama's default is too small, so pull a model and then create a variant with 64k context:

```bash
ollama pull qwen3-coder:30b

cat > /tmp/Modelfile <<'EOF'
FROM qwen3-coder:30b
PARAMETER num_ctx 65536
EOF
ollama create qwen3-coder:30b-64k -f /tmp/Modelfile

ollama run qwen3-coder:30b-64k "Say hi in five words"
```

If that model isn't available or is too big for you, pick any model on ollama.com that has the **tools** tag and use its name everywhere below.

## 3. Get the code

```bash
git clone <this repo> ~/local-ai-mcp     # or unzip the folder to ~/local-ai-mcp
cd ~/local-ai-mcp
```

The rest of this tutorial assumes the code is in `~/local-ai-mcp`.

## 4. A second Claude Code config, for the local model

`local_delegate` runs Claude Code a second time, pointed at Ollama instead of Anthropic. That second copy gets its own config folder, so it never touches your normal login:

```bash
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
```

Test it. This is the exact kind of call the MCP makes:

```bash
CLAUDE_CONFIG_DIR=~/.claude-local claude -p "Reply with exactly: OK" \
  --model qwen3-coder:30b-64k
```

You should get `OK` after a few seconds (the first call also loads the model into memory). If you get an auth or connection error, check that Ollama is running and that the URL above is right.

## 5. Tell the server which models exist

Copy the example registry:

```bash
cd ~/local-ai-mcp && cp models.example.json models.json
```

It registers `qwen3-coder:30b-64k` under the alias `coding`. If you pulled a different model in step 2, change the `model` value to match.

The first entry is the default. The `note` is what Claude reads when it decides which model to use.

You don't need `extras.json` yet. Leave it out and the local agent runs without extra tools.

## 6. Register the MCP server and run the self-test

```bash
claude mcp add --scope user local-ai -- python3 ~/local-ai-mcp/server.py
claude mcp list | grep local-ai        # ✔ Connected

cd ~/local-ai-mcp && python3 server.py selftest
```

The self-test speaks MCP to the server and runs one `local_ask`. It should end with `selftest passed`.

## 7. Open the dashboard

```bash
python3 ~/local-ai-mcp/server.py dashboard
```

Open http://127.0.0.1:8765. You'll see the self-test call under **Recent calls**. Leave this terminal running. (Later on you can skip this step: the MCP server starts the dashboard by itself on the first tool call.)

Click **reset session** next to *This session*. The session tiles drop to zero, so they'll show only what this tutorial uses.

## 8. First call: `local_ask`

In another terminal, start Claude Code in any project:

```bash
cd ~/some-project && claude
```

Ask:

> Use local_ask to write a haiku about merge conflicts.

Look at the dashboard: a **Live** card appears with the prompt and the answer as it streams in. When it's done, the call moves to **Recent calls**, and the session tiles show the tokens it used. Those tokens were free.

Something more useful:

> Run `git log --oneline -30` and use local_ask to group these commits into a short changelog.

## 9. Second call: `local_delegate` (read-only)

Now a real agent on the local model:

> Use local_delegate in this repo: find where configuration is loaded and list the files with line numbers. Max 10 lines.

On the live card you'll see the agent's turns: `→ Grep …`, `→ Read …`, results coming back, token counts per turn. It can only read: Edit, Write and Bash are not available to it.

Tips for good briefs (local models are fast but literal):
- Name exact files, functions and identifiers.
- Ask for a short answer ("path:line list", "max 5 lines").
- One task per call.

## 10. Let it edit

> Use local_delegate with allow_edits to add a `--version` flag to the CLI in src/cli.py. Reply "done" plus the changed files.

Afterwards, have Claude check the result (`git diff --stat`, run the tests). The local model does the typing; Claude checks the result.

**Stopping a call:** click **■ Stop** on the live card. Claude gets "cancelled by the user" and won't retry on its own.

## 11. Orchestrator mode (optional)

Want Claude to hand off *everything* hands-on and only plan and check? Install the skill:

```bash
mkdir -p ~/.claude/skills
cp -r ~/local-ai-mcp/skill/local-ai ~/.claude/skills/
sed -i '' "s#/path/to/local-ai-mcp#$HOME/local-ai-mcp#" ~/.claude/skills/local-ai/SKILL.md
```

Then, in a new Claude Code session:

```
/local-ai
Add input validation to the signup form and a test for it.
```

Claude now writes briefs, the local model does the work, and Claude runs the build and checks the diff. `/local-ai off` switches it back.

## 12. Read the dashboard

- **This session** vs **All time**: click *reset session* before a task to see what that task cost.
- **Recent calls**: click a row to see the output. The prompt is collapsed at the top of the row; click it to open. *collapse all* closes everything again.
- **A red live card** means nothing visible has happened for a minute. If the GPU is busy, the model is probably looping inside a tool call: stop it and give it a narrower brief. If the GPU is idle, it's waiting on something else.
- **Models**: add more aliases (say, a big model for long files), make one the default, and load or unload them from memory. Restart Claude Code after changing models so it sees the new list.

## You're done 🎉

Next steps:
- Try a second model, register it in the dashboard, and compare speed with `python3 bench.py <alias>`.
- Give the local agent extra tools through `extras.json` (copy `extras.example.json`) plus `/local-ai <name>` (see README → `extras.json`).
- Something broken? See README → **Troubleshooting**.
