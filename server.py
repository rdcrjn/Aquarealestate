"""
Jotform MCP server (Streamable HTTP, no dependencies).

Env vars:
  JOTFORM_API_KEY  your Jotform API key (required)
  MCP_TOKEN        a secret you make up; OpenAI sends it as the access token (required)
  FORM_ID          default form (optional, default 212514536993158)
  JOTFORM_BASE     optional, e.g. https://eu-api.jotform.com
  PORT             set automatically by Render/Railway (default 8000)

Chat UI (served at /):
  OPENAI_API_KEY   your OpenAI API key (required for the chat page)
  APP_PASSWORD     password people type to use the chat page (strongly recommended)
  OPENAI_MODEL     optional, default gpt-6-astra
  PUBLIC_URL       optional; Render sets RENDER_EXTERNAL_URL automatically
"""
import json, os, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_KEY = os.environ.get("JOTFORM_API_KEY", "")
TOKEN = os.environ.get("MCP_TOKEN", "")
FORM_ID = os.environ.get("FORM_ID", "212514536993158")
BASE = os.environ.get("JOTFORM_BASE", "https://api.jotform.com").rstrip("/")

TOOLS = [
    {
        "name": "list_forms",
        "description": "List the Jotform forms in the account: id, title, status, submission count, created_at and url.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max 1000, default 100"},
                "offset": {"type": "integer", "description": "For paging, default 0"},
            },
        },
    },
    {
        "name": "get_form_questions",
        "description": "List the questions (fields) on a form: question id, label, type and whether it is required.",
        "inputSchema": {
            "type": "object",
            "properties": {"form_id": {"type": "string", "description": f"Form ID (default {FORM_ID})"}},
        },
    },
    {
        "name": "list_submissions",
        "description": "Get submissions for a Jotform form, newest first. Each item has id, created_at and answers (question -> answer).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "form_id": {"type": "string", "description": f"Form ID (default {FORM_ID})"},
                "limit": {"type": "integer", "description": "Max 1000, default 100"},
                "offset": {"type": "integer", "description": "For paging, default 0"},
            },
        },
    },
    {
        "name": "get_submission",
        "description": "Get one Jotform submission by its submission ID.",
        "inputSchema": {
            "type": "object",
            "properties": {"submission_id": {"type": "string"}},
            "required": ["submission_id"],
        },
    },
]


def jotform(path, params=None):
    q = urllib.parse.urlencode(params or {})
    req = urllib.request.Request(f"{BASE}{path}?{q}", headers={"APIKEY": API_KEY})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["content"]


def simplify(sub):
    answers = {}
    for a in (sub.get("answers") or {}).values():
        q = a.get("text") or a.get("name")
        v = a.get("prettyFormat") or a.get("answer")
        if q and v not in (None, "", {}, []):
            answers[q] = v
    return {"id": sub.get("id"), "form_id": sub.get("form_id"),
            "created_at": sub.get("created_at"), "answers": answers}


def call_tool(name, args):
    if name == "list_forms":
        limit = max(1, min(int(args.get("limit") or 100), 1000))
        data = jotform("/user/forms", {"limit": limit, "offset": int(args.get("offset") or 0),
                                       "orderby": "created_at"})
        forms = [{"id": f.get("id"), "title": f.get("title"), "status": f.get("status"),
                  "submissions": f.get("count"), "created_at": f.get("created_at"),
                  "url": f.get("url")} for f in data]
        return {"count": len(forms), "forms": forms}
    if name == "get_form_questions":
        data = jotform(f"/form/{args.get('form_id') or FORM_ID}/questions")
        qs = [{"qid": q.get("qid"), "label": q.get("text"), "type": q.get("type"),
               "required": q.get("required") == "Yes"}
              for q in sorted(data.values(), key=lambda q: int(q.get("order") or 0))
              if q.get("text") and q.get("type") not in ("control_head", "control_button",
                                                         "control_pagebreak", "control_text")]
        return {"count": len(qs), "questions": qs}
    if name == "list_submissions":
        limit = max(1, min(int(args.get("limit") or 100), 1000))
        data = jotform(f"/form/{args.get('form_id') or FORM_ID}/submissions",
                       {"limit": limit, "offset": int(args.get("offset") or 0),
                        "orderby": "created_at"})
        return {"count": len(data), "submissions": [simplify(s) for s in data]}
    if name == "get_submission":
        return simplify(jotform(f"/submission/{args['submission_id']}"))
    raise ValueError(f"Unknown tool: {name}")


def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:  # notification
        return None
    if method == "initialize":
        result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-03-26"),
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "jotform", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        p = msg.get("params", {})
        try:
            out = call_tool(p.get("name"), p.get("arguments") or {})
            result = {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}]}
        except Exception as e:
            result = {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


# ---------------- Chat UI (talks to OpenAI, which uses the /mcp tools) ----------------
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
MODEL = os.environ.get("OPENAI_MODEL", "gpt-6-astra")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")

INSTRUCTIONS = """You are Aqua Assist. You answer questions about property-listing submissions from Jotform.
- Tools: list_forms (all forms), get_form_questions (a form's fields), list_submissions (a form's submissions; default form 212514536993158), get_submission (one by ID).
- For questions about submissions, call list_submissions (limit 1000) to get current data. If 1000 come back, call again with offset 1000, 2000 and so on.
- Each submission has id, created_at and answers (question -> answer).
- Find a client by name, email, phone, Deal ID or submission ID. Use get_submission when given an ID.
- The same question can be worded differently across submissions; check all matching questions.
- If an answer is missing, say "Not answered". Never guess.
- Keep answers short. Use a markdown table when comparing several clients."""

PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Aqua Assist</title><style>
*{box-sizing:border-box}body{margin:0;font-family:system-ui,sans-serif;background:#f4f7fa;color:#1d2733;display:flex;flex-direction:column;height:100vh}
header{background:#0b6e99;color:#fff;padding:14px 20px;font-weight:600;font-size:18px}
#log{flex:1;overflow:auto;padding:20px;max-width:860px;width:100%;margin:0 auto}
.m{padding:10px 14px;border-radius:12px;margin:8px 0;max-width:85%;white-space:pre-wrap;line-height:1.45}
.u{background:#0b6e99;color:#fff;margin-left:auto}.a{background:#fff;border:1px solid #dde3ea}
.a table{border-collapse:collapse;margin:6px 0;white-space:normal}.a td,.a th{border:1px solid #dde3ea;padding:4px 8px;text-align:left}
form{display:flex;gap:8px;padding:14px;max-width:860px;width:100%;margin:0 auto}
input{flex:1;padding:12px;border:1px solid #c9d2dc;border-radius:10px;font-size:15px}
button{padding:12px 18px;border:0;border-radius:10px;background:#0b6e99;color:#fff;font-size:15px;cursor:pointer}
button:disabled{opacity:.5}</style></head><body>
<header>Aqua Assist</header><div id="log"><div class="m a">Hi! Ask me anything about your Jotform listing submissions.</div></div>
<form id="f"><input id="q" placeholder="e.g. List all clients with email and phone" autocomplete="off"><button id="b">Send</button></form>
<script>
let prev=null,pw=sessionStorage.getItem('pw')||'';const log=document.getElementById('log');
function esc(s){return s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function md(t){t=esc(t);const L=t.split('\\n'),o=[];let tb=[];const flush=()=>{if(!tb.length)return;
 const r=tb.filter(x=>!/^[\\s|:-]+$/.test(x));
 o.push('<table>'+r.map((x,i)=>'<tr>'+x.replace(/^\\||\\|$/g,'').split('|').map(c=>i?'<td>'+c.trim()+'</td>':'<th>'+c.trim()+'</th>').join('')+'</tr>').join('')+'</table>');tb=[]};
 for(const l of L){if(l.trim().startsWith('|'))tb.push(l.trim());else{flush();o.push(l)}}flush();
 return o.join('\\n').replace(/\\*\\*(.+?)\\*\\*/g,'<b>$1</b>')}
function add(t,c){const d=document.createElement('div');d.className='m '+c;d.innerHTML=c=='a'?md(t):esc(t);log.appendChild(d);log.scrollTop=1e9;return d}
document.getElementById('f').onsubmit=async e=>{e.preventDefault();const q=document.getElementById('q'),b=document.getElementById('b');
 const msg=q.value.trim();if(!msg)return;q.value='';add(msg,'u');const w=add('Thinking…','a');b.disabled=true;
 for(let i=0;i<2;i++){const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({message:msg,previous_response_id:prev,password:pw})});const j=await r.json();
  if(r.status==401){pw=prompt('Password')||'';sessionStorage.setItem('pw',pw);continue}
  w.innerHTML=md(j.reply||('Error: '+(j.error||r.status)));if(j.response_id)prev=j.response_id;break}
 b.disabled=false;q.focus()};
</script></body></html>"""


def ask_openai(message, previous_id=None):
    body = {
        "model": MODEL,
        "instructions": INSTRUCTIONS,
        "input": message,
        "tools": [{
            "type": "mcp", "server_label": "jotform",
            "server_url": f"{PUBLIC_URL}/mcp",
            "headers": {"Authorization": f"Bearer {TOKEN}"},
            "require_approval": "never",
        }],
    }
    if previous_id:
        body["previous_response_id"] = previous_id
    req = urllib.request.Request("https://api.openai.com/v1/responses", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(e.read().decode()[:500])
    text = "".join(c.get("text", "") for item in data.get("output", []) if item.get("type") == "message"
                   for c in item.get("content", []) if c.get("type") == "output_text")
    return text or "(no reply)", data.get("id")


class H(BaseHTTPRequestHandler):
    def _send(self, code, body=None, html=None):
        self.send_response(code)
        if html is not None:
            data = html.encode()
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif body is not None:
            data = json.dumps(body).encode()
            self.send_header("Content-Type", "application/json")
        else:
            data = b""
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            return self._send(200, html=PAGE)
        if self.path == "/health":
            return self._send(200, {"status": "ok", "mcp": "/mcp"})
        self._send(405)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
        if self.path == "/chat":
            if APP_PASSWORD and body.get("password") != APP_PASSWORD:
                return self._send(401, {"error": "Wrong password"})
            try:
                reply, rid = ask_openai(str(body.get("message", ""))[:4000], body.get("previous_response_id"))
                return self._send(200, {"reply": reply, "response_id": rid})
            except Exception as e:
                return self._send(500, {"error": str(e)})
        if TOKEN and self.headers.get("Authorization", "") != f"Bearer {TOKEN}":
            return self._send(401, {"error": "unauthorized"})
        if isinstance(body, list):
            out = [r for r in (handle(m) for m in body) if r]
        else:
            out = handle(body)
        self._send(200, out) if out else self._send(202)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"Jotform MCP listening on :{port}/mcp")
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
