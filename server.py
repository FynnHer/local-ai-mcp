#!/usr/bin/env python3
"""Local AI MCP server + stats dashboard. Python stdlib only.

  python3 server.py              MCP server over stdio (register it in Claude Code)
  python3 server.py dashboard    stats, live runs + model registry at http://127.0.0.1:8765
  python3 server.py selftest     talks MCP to itself and runs one local_ask

The MCP server starts the dashboard and opens it in the browser on its first tool call
(set LOCAL_AI_OPEN=0 to disable).
"""
import fcntl, glob, http.client, json, os, re, shutil, socket, sqlite3, subprocess, sys, tempfile, threading, time, urllib.request, webbrowser
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODELS = HERE / "models.json"
EXTRAS = HERE / "extras.json"  # MCP servers and skills a delegate may opt into
DB = HERE / "stats.db"
LOCK = HERE / ".ollama.lock"  # one model call at a time, across all MCP servers
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
LOCAL_CFG = os.path.expanduser(os.environ.get("LOCAL_CLAUDE_CONFIG", "~/.claude-local"))
CLAUDE_BIN = shutil.which("claude") or "/opt/homebrew/bin/claude"
PORT = int(os.environ.get("DASHBOARD_PORT", "8765"))
TIMEOUT = 1800  # s, local models are slow
LIVE_MAX = 50_000  # chars of live output kept per run


# ---- model registry: models.json, first entry is the default ----

def load_models():
    try:
        return json.loads(MODELS.read_text())
    except FileNotFoundError:
        return {}


def save_models(m):
    MODELS.write_text(json.dumps(m, indent=2) + "\n")


def resolve(alias):
    models = load_models()
    if not models:
        raise ValueError("no models registered, add one in the dashboard or models.json")
    if not alias:
        return next(iter(models.values()))["model"]
    if alias in models:
        return models[alias]["model"]
    raise ValueError(f"unknown model {alias!r}, known: {', '.join(models)}")


# ---- ollama + run log ----

def ollama(path, body=None, timeout=TIMEOUT):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(OLLAMA + path, data=data, headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def ollama_json(path, timeout=3):
    with ollama(path, timeout=timeout) as r:
        return json.load(r)


def installed():
    return sorted(m["name"] for m in ollama_json("/api/tags")["models"])


def db():
    c = sqlite3.connect(DB, timeout=10)
    c.execute("""CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, ts REAL, tool TEXT, model TEXT,
                 status TEXT, pid INT, prompt TEXT, live TEXT DEFAULT '', prompt_tokens INT DEFAULT 0,
                 gen_tokens INT DEFAULT 0, gen_ms REAL DEFAULT 0, total_ms REAL DEFAULT 0, cancel INT DEFAULT 0,
                 cached_tokens INT DEFAULT 0, phase TEXT DEFAULT '', phase_ts REAL DEFAULT 0, last_out REAL DEFAULT 0)""")
    # stats.db files from before these columns existed
    for col in ("cancel INT DEFAULT 0", "cached_tokens INT DEFAULT 0", "phase TEXT DEFAULT ''", "phase_ts REAL DEFAULT 0",
                "last_out REAL DEFAULT 0"):
        try:
            c.execute(f"ALTER TABLE runs ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v)")  # session_start: reset from the dashboard
    return c


class Run:
    """One tool call as a row in `runs`, saved while it runs so the dashboard can follow along.

    The dashboard is another process, so it cancels by setting runs.cancel; a watcher thread
    polls that flag and calls on_cancel (kill the subprocess / cut the Ollama connection)."""

    def __init__(self, tool, model, prompt):
        self.t, self.first, self.last, self.live, self.gen = time.time(), None, 0.0, "", 0
        self.inp = self.cached = 0
        self.phase, self.phase_ts, self.last_out = "starting", self.t, self.t
        self.cancelled, self.on_cancel, self._lock = False, None, None
        c = db()
        with c:
            self.id = c.execute("INSERT INTO runs(ts, tool, model, status, pid, prompt) VALUES(?,?,?,?,?,?)",
                                (self.t, tool, model, "running", os.getpid(), prompt)).lastrowid
        c.close()
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self):
        while True:
            time.sleep(1)
            c = db()
            status, cancel = c.execute("SELECT status, cancel FROM runs WHERE id = ?", (self.id,)).fetchone()
            c.close()
            if status != "running":
                return
            if cancel and self.on_cancel:
                self.cancelled = True
                self.on_cancel()
                return

    def check_cancelled(self):
        if self.cancelled:
            raise RuntimeError("cancelled by the user from the dashboard, don't retry without asking")

    def wait_turn(self):
        """Wait for the model. Ollama runs one request at a time and would otherwise queue the rest
        invisibly; here a waiting call shows as 'queued' and can be stopped while it waits."""
        # ponytail: flock isn't strictly FIFO, fine for a handful of waiting calls
        self._lock = open(LOCK, "w")
        while True:
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                self.set_phase("queued")
                c = db()
                cancel = c.execute("SELECT cancel FROM runs WHERE id = ?", (self.id,)).fetchone()[0]
                c.close()
                if cancel:
                    self.cancelled = True
                    self.check_cancelled()
                time.sleep(0.5)

    def save(self, **cols):
        if self.first:
            cols.setdefault("gen_ms", (time.time() - self.first) * 1000)
        cols = {"live": self.live[-LIVE_MAX:], "gen_tokens": self.gen, "prompt_tokens": self.inp,
                "cached_tokens": self.cached, "phase": self.phase, "phase_ts": self.phase_ts,
                "last_out": self.last_out, **cols}
        c = db()
        with c:
            c.execute(f"UPDATE runs SET {', '.join(k + '=?' for k in cols)} WHERE id=?", (*cols.values(), self.id))
        c.close()

    def tick(self):  # throttled save; every call means the model visibly produced something
        self.first = self.first or time.time()
        self.last_out = time.time()
        if time.time() - self.last >= 0.5:
            self.last = time.time()
            self.save()

    def set_phase(self, phase):  # what the model is doing right now, shown on the live card
        if phase != self.phase:
            self.phase, self.phase_ts = phase, time.time()
            self.last_out = self.phase_ts
            self.save()

    def finish(self, status, **cols):
        if self._lock:
            self._lock.close()
            self._lock = None
        self.save(status=status, total_ms=(time.time() - self.t) * 1000, **cols)

    def fail(self, e):
        if self.cancelled:
            self.live += "\n\n■ stopped from the dashboard"
            return self.finish("cancelled")
        self.live += f"\n\nERROR: {type(e).__name__}: {e}"
        self.finish("error")


# ---- MCP tools ----

def local_ask(prompt, model=None, system=None, think=False):
    m = resolve(model)
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    run = Run("ask", m, prompt)
    # http.client instead of urlopen so the cancel watcher can shut the socket; Ollama stops on disconnect.
    conn = http.client.HTTPConnection(urlsplit(OLLAMA).netloc, timeout=TIMEOUT)
    run.on_cancel = lambda: conn.sock and conn.sock.shutdown(socket.SHUT_RDWR)
    try:
        thinking, answer, last = "", "", {}
        run.wait_turn()
        run.set_phase("reading prompt")
        # num_predict caps runaway loops; 16k still leaves room for long thinking
        body = {"model": m, "messages": msgs, "stream": True, "think": think, "options": {"num_predict": 16384}}
        conn.request("POST", "/api/chat", json.dumps(body), {"Content-Type": "application/json"})
        r = conn.getresponse()
        if r.status != 200:
            raise RuntimeError(f"ollama {r.status}: {r.read()[:300].decode(errors='replace')}")
        for line in r:
            last = json.loads(line)
            msg = last.get("message", {})
            thinking += msg.get("thinking", "")
            answer += msg.get("content", "")
            if msg.get("thinking") or msg.get("content"):
                run.set_phase("answering" if msg.get("content") else "thinking")
            run.gen += 1  # one chunk ~ one token, exact count arrives with the last chunk
            run.live = (f"[thinking]\n{thinking.strip()}\n[/thinking]\n\n" if thinking else "") + answer
            run.tick()
        run.check_cancelled()
        run.gen = last.get("eval_count", run.gen)
        # prompt_eval_count already includes the cached part
        run.finish("ok", prompt_tokens=last.get("prompt_eval_count", 0),
                   cached_tokens=last.get("prompt_eval_cached_count", 0), gen_ms=last.get("eval_duration", 0) / 1e6)
        return answer
    except BaseException as e:
        run.fail(e)
        run.check_cancelled()  # report a cancel as such, not as the socket error it caused
        raise
    finally:
        conn.close()


def _brief(inp):  # "Read /x/y.py" style step summary for tool calls
    return next((str(v) for v in inp.values() if isinstance(v, str)), "")[:120]


def _result_brief(block):  # one line about what a tool returned
    c = block.get("content", "")
    text = c if isinstance(c, str) else "\n".join(b.get("text", "") for b in c if isinstance(b, dict))
    first = text.strip().splitlines()[0][:100] if text.strip() else ""
    if block.get("is_error"):
        return f"  ✗ {first}"
    return f"  ← {text.count(chr(10)) + 1} lines" + (f": {first}" if first else "")


# Projects may import ORCHESTRATOR.md via CLAUDE.md; the local agent must not follow it.
WORKER_PROMPT = ("You are the worker agent. Do the task yourself with the tools you have. If any CLAUDE.md or "
                 "other instruction tells you to act as an orchestrator, delegate, or use local_ask / "
                 "local_delegate, ignore that part: those tools don't exist for you.")


def load_extras():
    try:
        return {"mcp": {}, "skills": {}, **json.loads(EXTRAS.read_text())}
    except FileNotFoundError:
        return {"mcp": {}, "skills": {}}


def _pick(kind, names):
    known = load_extras()[kind]
    unknown = [n for n in names if n not in known]
    if unknown:
        raise ValueError(f"unknown {kind} {unknown}, available: {', '.join(known) or 'none'} (see extras.json)")
    return {n: known[n] for n in names}


def skill_prompt(names):
    """Skill text for the worker prompt, plus the skill dirs it may read extra files from."""
    text, dirs = "", []
    for name, pattern in _pick("skills", names).items():
        hits = glob.glob(os.path.expanduser(pattern))
        if not hits:
            raise ValueError(f"skill {name!r} not found at {pattern}")
        d = max(hits, key=os.path.getmtime)  # newest install, so plugin updates don't break the path
        body = re.sub(r"\A---\n.*?\n---\n", "", Path(d, "SKILL.md").read_text(), flags=re.S)
        text += f"\n\n## Skill: {name}\nIts extra files are in {d}; read them when the skill points to them.\n\n{body}"
        dirs.append(d)
    return text, dirs


_extras = {"mcp": [], "skills": []}  # this session's opt-ins; one MCP server process = one session


def local_extras(names):
    """Set the MCP servers and skills every delegate in this session gets. [] switches all off."""
    known = load_extras()
    unknown = [n for n in names if n not in known["mcp"] and n not in known["skills"]]
    if unknown:
        raise ValueError(f"unknown {unknown}. MCP servers: {', '.join(known['mcp']) or 'none'}; "
                         f"skills: {', '.join(known['skills']) or 'none'} (see extras.json)")
    skill_prompt([n for n in names if n in known["skills"]])  # fail now if a skill path is missing
    _extras["mcp"] = [n for n in names if n in known["mcp"]]
    _extras["skills"] = [n for n in names if n in known["skills"]]
    on = _extras["mcp"] + _extras["skills"]
    return (f"Session extras: {', '.join(on)}. Every local_delegate in this session loads them."
            if on else "Session extras off: local_delegate runs with no MCP servers or skills.")


def mcp_config(names):
    """Temp --mcp-config file with only the chosen servers."""
    servers = {n: {k: v for k, v in cfg.items() if k != "note"} for n, cfg in _pick("mcp", names).items()}
    f = tempfile.NamedTemporaryFile("w", suffix=".json", prefix="local-ai-mcp-", delete=False)
    json.dump({"mcpServers": servers}, f)
    f.close()
    return f.name


def _usage_in(u):
    return sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


def local_delegate(task, cwd, model=None, allow_edits=False):
    m = resolve(model)
    if not os.path.isdir(cwd):
        raise ValueError(f"cwd {cwd!r} is not a directory")
    # Drop the work session's env (proxy URL, token, OTEL) so the child only uses the local config.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE", "OTEL_"))}
    env["CLAUDE_CONFIG_DIR"] = LOCAL_CFG
    # A looping model can generate silently for many minutes (Ollama holds back unfinished
    # tool calls); 8k tokens per turn still fits writing a whole file.
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = "8192"
    tools = "Read,Grep,Glob" + (",Edit,Write" if allow_edits else "")
    # Only the tools it may use, no MCP servers or skills: every tool definition is prompt the local
    # model re-reads each turn. Dynamic sections go last so Ollama can reuse the cached prefix.
    cmd = [CLAUDE_BIN, "-p", task, "--model", m, "--output-format", "stream-json", "--verbose",
           "--include-partial-messages", f"--tools={tools}", "--strict-mcp-config", "--disable-slash-commands",
           "--exclude-dynamic-system-prompt-sections",
           "--permission-mode", "acceptEdits" if allow_edits else "default"]
    # Off by default, opted into per session (local_extras): every MCP tool and skill is prompt
    # the local model re-reads each turn.
    mcp, skills = _extras["mcp"], _extras["skills"]
    extra, skill_dirs = skill_prompt(skills)
    cmd += ["--append-system-prompt", WORKER_PROMPT + extra] + [f"--add-dir={d}" for d in skill_dirs]
    cfg_path = mcp_config(mcp) if mcp else None
    if cfg_path:
        cmd += [f"--mcp-config={cfg_path}", "--allowedTools=" + ",".join(f"mcp__{n}" for n in mcp)]
    run = Run("delegate", m, task)
    run.live = f"started in {cwd}, tools: {tools}" + (f", mcp: {', '.join(mcp)}" if mcp else "") \
        + (f", skills: {', '.join(skills)}" if skills else "") + "\n"
    run.save()
    try:
        run.wait_turn()
        run.set_phase("starting claude")
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        run.on_cancel = p.kill
        killer = threading.Timer(TIMEOUT, p.kill)
        killer.start()
        result, noise = None, []
        turn, turn_in, turn_cached, gen_before, block = 0, 0, 0, 0, None
        for line in p.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                noise.append(line)
                continue
            kind = ev.get("type")
            if kind == "system" and ev.get("status") == "requesting":
                run.set_phase(f"turn {turn + 1}: reading prompt")
            elif kind == "stream_event":  # token-level events from --include-partial-messages
                e = ev.get("event", {})
                et = e.get("type")
                if et == "message_start":
                    # Ollama only estimates input here (no cache info); message_delta has the real numbers.
                    turn, gen_before, turn_cached = turn + 1, run.gen, 0
                    turn_in = _usage_in(e.get("message", {}).get("usage") or {})
                    run.inp += turn_in
                    run.live += f"\n── turn {turn} ──\n"
                    run.set_phase(f"turn {turn}: generating")
                elif et == "content_block_start":
                    cb = e.get("content_block", {})
                    block = cb.get("type")
                    if block == "thinking":
                        run.live += "[thinking] "
                        run.set_phase(f"turn {turn}: thinking")
                    elif block == "text":
                        run.set_phase(f"turn {turn}: writing")
                    elif block == "tool_use":
                        run.set_phase(f"turn {turn}: preparing {cb.get('name')} call")
                elif et == "content_block_delta":
                    d = e.get("delta", {})
                    run.live += d.get("text", "") + d.get("thinking", "")
                    run.gen += 1  # ~one token per delta, corrected by message_delta
                elif et == "content_block_stop" and block in ("thinking", "text"):
                    run.live += "\n"
                elif et == "message_delta":
                    u = e.get("usage") or {}
                    if u.get("output_tokens"):
                        run.gen = gen_before + u["output_tokens"]
                    if _usage_in(u):  # swap the start-of-turn estimate for the real counts
                        real_in, real_cached = _usage_in(u), u.get("cache_read_input_tokens") or 0
                        run.inp += real_in - turn_in
                        run.cached += real_cached - turn_cached
                        turn_in, turn_cached = real_in, real_cached
                    run.live += f"   (turn {turn}: read {turn_in:,} tokens, {turn_cached:,} cached · " \
                                f"wrote {run.gen - gen_before:,})\n"
                run.tick()
            elif kind == "assistant":  # complete message: text already streamed, only list the tool calls
                for b in ev.get("message", {}).get("content", []):
                    if b.get("type") == "tool_use":
                        run.live += f"→ {b.get('name')} {_brief(b.get('input', {}))}\n"
                        run.set_phase(f"turn {turn}: running {b.get('name')}")
                run.tick()
            elif kind == "user":  # tool results going back to the model
                content = ev.get("message", {}).get("content", [])
                for b in content if isinstance(content, list) else []:
                    if b.get("type") == "tool_result":
                        run.live += _result_brief(b) + "\n"
                run.tick()
            elif kind == "result":
                result = ev
        p.wait()
        killer.cancel()
        run.check_cancelled()
        if result is None:
            raise RuntimeError(f"local claude failed (exit {p.returncode}): {''.join(noise)[-800:]}")
        u = result.get("usage", {})
        prompt_tokens = _usage_in(u)
        if result.get("is_error"):
            raise RuntimeError(result.get("result") or "local claude reported an error")
        run.live += "\n── result ──\n" + result.get("result", "")
        run.gen = u.get("output_tokens", 0)
        # ponytail: gen_ms is API time incl. prompt processing, so delegate tok/s reads low vs local_ask
        run.finish("ok", prompt_tokens=prompt_tokens, cached_tokens=u.get("cache_read_input_tokens", 0),
                   gen_ms=result.get("duration_api_ms", 0))
        return result.get("result", "")
    except BaseException as e:
        run.fail(e)
        run.check_cancelled()
        raise
    finally:
        if cfg_path:
            os.unlink(cfg_path)


def tool_defs():
    models = load_models()
    names = ", ".join(f"{a}{' (default)' if i == 0 else ''}: {v.get('note') or v['model']}"
                      for i, (a, v) in enumerate(models.items())) or "none registered"
    model_prop = {"type": "string", "description": f"Model alias, omit for default. Available: {names}"}
    extras = load_extras()
    available = [f"{n} (MCP server: {c.get('note', '')})" for n, c in extras["mcp"].items()] + \
                [f"{n} (skill)" for n in extras["skills"]]
    return [
        {"name": "local_ask",
         "description": "One-shot call to a local LLM (Ollama, free, private, ~60-90 tok/s). Use for summarising "
                        "text or logs you paste in, classifying, rewording, drafting boilerplate. No file access. "
                        "Put large shared context first and the question last: repeated prefixes are cached.",
         "inputSchema": {"type": "object", "required": ["prompt"], "properties": {
             "prompt": {"type": "string"}, "system": {"type": "string"}, "model": model_prop,
             "think": {"type": "boolean", "default": False,
                       "description": "Let the model reason first. Slower; only for logic or tricky analysis."}}}},
        {"name": "local_delegate",
         "description": "Run a full Claude Code agent on a local model in `cwd` and get its final answer. It can "
                        "Read/Grep/Glob the repo itself, so give it read-heavy work (explore code, find usages, "
                        "digest files) with a specific brief and ask for a concise answer. Bash is denied; edits "
                        "only with allow_edits. Slower and weaker than you: verify anything important.",
         "inputSchema": {"type": "object", "required": ["task", "cwd"], "properties": {
             "task": {"type": "string"}, "cwd": {"type": "string", "description": "Absolute path to work in"},
             "allow_edits": {"type": "boolean", "default": False}, "model": model_prop}}},
        {"name": "local_extras",
         "description": "Set which MCP servers and skills local_delegate loads for the rest of this session. Off by "
                        "default; only call this when the user asks for them. Replaces the current set; [] switches "
                        "everything off. Each one adds prompt the local model re-reads every turn. Available: "
                        + ("; ".join(available) or "none (add some to extras.json)"),
         "inputSchema": {"type": "object", "required": ["names"], "properties": {
             "names": {"type": "array", "items": {"type": "string"}}}}},
    ]


TOOLS = {"local_ask": local_ask, "local_delegate": local_delegate, "local_extras": local_extras}
_out = threading.Lock()
_opened = False


def send(msg):
    with _out:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def ensure_dashboard():
    """Once per MCP session: start the dashboard if nothing listens on PORT, then open it."""
    global _opened
    if _opened or os.environ.get("LOCAL_AI_OPEN") == "0":
        return
    _opened = True
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/ping", timeout=1) as r:
            if time.time() - json.load(r)["last_poll"] < 15:
                return  # a dashboard tab is already open and polling
    except OSError:
        subprocess.Popen([sys.executable, str(HERE / "server.py"), "dashboard"], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.7)
    webbrowser.open(f"http://127.0.0.1:{PORT}")


_session_busy = threading.Lock()  # one MCP server process = one work-Claude session


def busy_message():
    c = db()
    r = c.execute("SELECT id, phase, ? - ts FROM runs WHERE status = 'running' AND pid = ? ORDER BY ts LIMIT 1",
                  (time.time(), os.getpid())).fetchone()
    c.close()
    where = f"run #{r[0]}, {r[1] or 'starting'}, {r[2]:.0f}s so far" if r else "just started"
    return (f"REFUSED: your previous local-ai call is still running ({where}). Nothing was started. "
            "Wait for its result before doing anything else: don't read or check files (they are half-done until "
            "it reports), don't run commands, don't send another brief. Its result will arrive on its own.")


def call_tool(id_, params):
    name, args = params.get("name"), params.get("arguments") or {}
    # Claude Code backgrounds slow tool calls, and the orchestrator then tends to inspect half-written
    # files and fire the next brief. A session gets one call at a time; other sessions use the queue.
    if name == "local_extras":  # instant settings change, not a model call
        try:
            text, err = local_extras(**args), False
        except Exception as e:
            text, err = f"{type(e).__name__}: {e}", True
        send({"jsonrpc": "2.0", "id": id_, "result": {"content": [{"type": "text", "text": text}], "isError": err}})
        return
    if not _session_busy.acquire(blocking=False):
        text = busy_message()
        send({"jsonrpc": "2.0", "id": id_, "result": {"content": [{"type": "text", "text": text}], "isError": True}})
        return
    try:
        try:
            ensure_dashboard()
        except OSError:
            pass  # dashboard is a nice-to-have, never fail the tool over it
        try:
            text, err = TOOLS[name](**args), False
        except Exception as e:  # tool errors go back to the model, not the protocol
            text, err = f"{type(e).__name__}: {e}", True
    finally:
        _session_busy.release()
    send({"jsonrpc": "2.0", "id": id_, "result": {"content": [{"type": "text", "text": text}], "isError": err}})


def serve_mcp():
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        method, id_ = msg.get("method"), msg.get("id")
        if method == "tools/call":
            threading.Thread(target=call_tool, args=(id_, msg.get("params", {})), daemon=True).start()
            continue
        if id_ is None:  # notification
            continue
        if method == "initialize":
            result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "local-ai", "version": "0.2"}}
        elif method == "tools/list":
            result = {"tools": tool_defs()}
        elif method == "ping":
            result = {}
        else:
            send({"jsonrpc": "2.0", "id": id_, "error": {"code": -32601, "message": f"unknown method {method}"}})
            continue
        send({"jsonrpc": "2.0", "id": id_, "result": result})


# ---- dashboard ----

ALIAS = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
DONE = "status != 'running'"


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def stats():
    c = db()
    c.row_factory = sqlite3.Row
    q = lambda sql, *a: [dict(r) for r in c.execute(sql, a)]
    summary = lambda since: q("""SELECT count(*) calls, coalesce(sum(status IN ('error','aborted')),0) errors,
                         coalesce(sum(prompt_tokens),0) prompt_tokens, coalesce(sum(gen_tokens),0) gen_tokens,
                         coalesce(sum(cached_tokens),0) cached_tokens,
                         coalesce(sum(prompt_tokens + gen_tokens),0) total_tokens,
                         coalesce(sum(total_ms),0) total_ms,
                         coalesce(sum(gen_tokens)*1000.0/nullif(sum(gen_ms),0),0) tps,
                         coalesce(sum(ts > strftime('%s','now','-1 day')),0) calls_24h FROM runs WHERE ts >= ?""", since)[0]
    with c:  # first dashboard start opens the first session
        c.execute("INSERT OR IGNORE INTO meta VALUES('session_start', ?)", (time.time(),))
    session_start = c.execute("SELECT v FROM meta WHERE k = 'session_start'").fetchone()[0]
    # Runs whose MCP server died (Claude session closed mid-call) would show as running forever.
    with c:
        for r in q("SELECT DISTINCT pid FROM runs WHERE status = 'running'"):
            if not alive(r["pid"]):
                c.execute("UPDATE runs SET status = 'aborted' WHERE status = 'running' AND pid = ?", (r["pid"],))
    out = {
        "summary": summary(0),
        "session": summary(session_start),
        "session_start": session_start,
        "per_model": q(f"""SELECT model, tool, count(*) calls, sum(status IN ('error','aborted')) errors,
                           sum(prompt_tokens) prompt_tokens, sum(cached_tokens) cached_tokens, sum(gen_tokens) gen_tokens,
                           sum(gen_tokens)*1000.0/nullif(sum(gen_ms),0) tps, avg(total_ms) avg_ms
                           FROM runs WHERE {DONE} GROUP BY model, tool ORDER BY calls DESC"""),
        "running": q("""SELECT id, ts, tool, model, substr(prompt, 1, 5000) prompt, length(prompt) prompt_len,
                        substr(live, -20000) live, gen_tokens, gen_ms, cancel, prompt_tokens, cached_tokens,
                        phase, phase_ts, last_out FROM runs WHERE status = 'running'
                        ORDER BY ts"""),
        "recent": q(f"""SELECT id, ts, tool, model, status, prompt_tokens, cached_tokens, gen_tokens, total_ms,
                        gen_tokens*1000.0/nullif(gen_ms,0) tps, substr(prompt, 1, 160) note
                        FROM runs WHERE {DONE} ORDER BY ts DESC LIMIT 50"""),
        "registry": load_models(),
        "now": time.time(),
    }
    c.close()
    queued = [r for r in out["running"] if r["phase"] == "queued"]
    for i, r in enumerate(queued, 1):  # running rows are ordered by start time
        r["queue_pos"] = i
    out["queued"] = len(queued)
    out.update(ollama_status(), gpu=gpu_busy())
    return out


_ollama_cache = (0.0, {})


def ollama_status():  # cached briefly: every open dashboard tab polls, Ollama's log filled up with it
    global _ollama_cache
    if time.time() - _ollama_cache[0] > 3:
        try:
            data = {"installed": installed(), "loaded": ollama_json("/api/ps")["models"], "ollama_up": True}
        except OSError:
            data = {"installed": [], "loaded": [], "ollama_up": False}
        _ollama_cache = (time.time(), data)
    return _ollama_cache[1]


def gpu_busy():
    """GPU utilisation in %, or None. Shows when a model is working without visible output."""
    try:  # ponytail: macOS only (ioreg)
        out = subprocess.run(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"],
                             capture_output=True, text=True, timeout=2).stdout
        m = re.search(r'"Device Utilization %"=(\d+)', out)
        return int(m.group(1)) if m else None
    except (OSError, subprocess.SubprocessError):
        return None


def run_detail(run_id):
    c = db()
    c.row_factory = sqlite3.Row
    r = c.execute("SELECT id, substr(prompt, 1, 200000) prompt, live FROM runs WHERE id = ?", (run_id,)).fetchone()
    c.close()
    return dict(r) if r else None


class Dashboard(BaseHTTPRequestHandler):
    last_poll = 0.0  # when a dashboard tab last asked for stats
    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self.reply(200, (HERE / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/stats":
            Dashboard.last_poll = time.time()
            self.reply(200, stats())
        elif self.path == "/api/ping":
            self.reply(200, {"last_poll": Dashboard.last_poll})
        elif m := re.fullmatch(r"/api/runs/(\d+)", self.path):
            r = run_detail(int(m.group(1)))
            self.reply(200 if r else 404, r or {"error": "not found"})
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self):
        # JSON content type forces a CORS preflight, which we never answer, so other sites can't post here.
        if self.headers.get("Content-Type") != "application/json":
            return self.reply(415, {"error": "application/json required"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if m := re.fullmatch(r"/api/runs/(\d+)/cancel", self.path):
            c = db()
            with c:
                hit = c.execute("UPDATE runs SET cancel = 1 WHERE id = ? AND status = 'running'",
                                (int(m.group(1)),)).rowcount
            c.close()
            return self.reply(200, {"ok": True}) if hit else self.reply(409, {"error": "that call isn't running"})
        if self.path == "/api/session/reset":
            c = db()
            with c:
                c.execute("INSERT OR REPLACE INTO meta VALUES('session_start', ?)", (time.time(),))
            c.close()
            return self.reply(200, {"ok": True})
        if m := re.fullmatch(r"/api/ollama/(load|unload)", self.path):
            model = str(body.get("model", ""))
            try:
                if model not in installed():
                    return self.reply(400, {"error": f"{model!r} is not installed in Ollama"})
                # Empty generate = load; keep_alive "2h" frees memory after 2 h idle (later calls reset it to Ollama's default)
                with ollama("/api/generate", {"model": model, "keep_alive": "2h" if m.group(1) == "load" else 0},
                            timeout=600) as r:
                    r.read()
            except OSError as e:
                return self.reply(502, {"error": f"ollama: {e}"})
            return self.reply(200, {"ok": True})
        models = load_models()
        if self.path == "/api/models":
            alias, model = str(body.get("alias", "")).strip(), str(body.get("model", "")).strip()
            if not ALIAS.match(alias):
                return self.reply(400, {"error": "alias: lowercase letters, digits, - or _, max 32"})
            if model not in installed():
                return self.reply(400, {"error": f"{model!r} is not installed in Ollama (ollama pull it first)"})
            models[alias] = {"model": model, "note": str(body.get("note", ""))[:120]}
        elif m := re.fullmatch(r"/api/models/([^/]+)/(default|delete)", self.path):
            alias, action = m.groups()
            if alias not in models:
                return self.reply(404, {"error": "unknown alias"})
            entry = models.pop(alias)
            if action == "default":
                models = {alias: entry, **models}
        else:
            return self.reply(404, {"error": "not found"})
        save_models(models)
        self.reply(200, models)

    def log_message(self, *a):
        pass


def selftest():
    env = {**os.environ, "LOCAL_AI_OPEN": "0"}
    p = subprocess.Popen([sys.executable, __file__], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
    rpc = lambda i, method, params=None: (p.stdin.write(json.dumps(
        {"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n"), p.stdin.flush(),
        json.loads(p.stdout.readline()))[2]
    assert rpc(1, "initialize")["result"]["serverInfo"]["name"] == "local-ai"
    assert {t["name"] for t in rpc(2, "tools/list")["result"]["tools"]} == set(TOOLS)
    r = rpc(3, "tools/call", {"name": "local_ask", "arguments": {"prompt": "Reply with exactly: OK"}})["result"]
    print("local_ask ->", r["content"][0]["text"][:200])
    assert not r["isError"]
    p.kill()
    last = stats()["recent"][0]
    assert last["tool"] == "ask" and last["status"] == "ok" and last["gen_tokens"] > 0, last
    print("selftest passed")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "mcp"
    if cmd == "dashboard":
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", PORT), Dashboard)
        except OSError as e:
            sys.exit(f"can't bind port {PORT} ({e.strerror}). Already running? Check `lsof -i :{PORT}` "
                     f"or pick another port with DASHBOARD_PORT=8766")
        print(f"dashboard on http://127.0.0.1:{PORT}")
        srv.serve_forever()
    elif cmd == "selftest":
        selftest()
    else:
        serve_mcp()
