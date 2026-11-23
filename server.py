"""
Jotform MCP server (Streamable HTTP, no dependencies).

Env vars:
  JOTFORM_API_KEY  your Jotform API key (required)
  MCP_TOKEN        a secret you make up; OpenAI sends it as the access token (required)
  FORM_ID          default form (optional, default 212514536993158)
  JOTFORM_BASE     optional, e.g. https://eu-api.jotform.com
  PORT             set automatically by Render/Railway (default 8000)
"""
import json, os, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_KEY = os.environ.get("JOTFORM_API_KEY", "")
TOKEN = os.environ.get("MCP_TOKEN", "")
FORM_ID = os.environ.get("FORM_ID", "212514536993158")
BASE = os.environ.get("JOTFORM_BASE", "https://api.jotform.com").rstrip("/")

TOOLS = [
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


class H(BaseHTTPRequestHandler):
    def _send(self, code, body=None):
        self.send_response(code)
        if body is not None:
            data = json.dumps(body).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_GET(self):
        if self.path == "/":
            return self._send(200, {"status": "ok", "mcp": "/mcp"})
        self._send(405)

    def do_POST(self):
        if TOKEN and self.headers.get("Authorization", "") != f"Bearer {TOKEN}":
            return self._send(401, {"error": "unauthorized"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
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
