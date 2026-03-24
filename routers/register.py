"""Registration + Admin router.

Public:
  GET  /           → Landing page (registration + API docs)
  POST /register   → Self-service API key registration
  GET  /my/{key}   → Student usage dashboard

Admin (protected by admin secret):
  GET  /admin                → Admin dashboard
  GET  /admin/api/keys       → All keys + usage
  POST /admin/api/keys/{key}/deactivate
  POST /admin/api/keys/{key}/activate
"""
import logging
import os
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from core.api_keys import APIKeyManager

logger = logging.getLogger("gemgate.register")
router = APIRouter(tags=["register"])

key_mgr: APIKeyManager = None
ADMIN_SECRET = os.environ.get("GEMGATE_ADMIN_SECRET", "")


def init(_key_mgr: APIKeyManager):
    global key_mgr
    key_mgr = _key_mgr


# ── Registration ──

class RegisterRequest(BaseModel):
    student_name: str

class RegisterResponse(BaseModel):
    success: bool
    api_key: str = ""
    student_name: str = ""
    message: str = ""
    base_url: str = ""

@router.post("/register", response_model=RegisterResponse)
async def register(req: RegisterRequest, request: Request):
    try:
        api_key = key_mgr.register(req.student_name)
    except ValueError as e:
        return RegisterResponse(success=False, message=str(e))

    base_url = str(request.base_url).rstrip("/")
    return RegisterResponse(
        success=True,
        api_key=api_key.key,
        student_name=api_key.student_name,
        message="API Key created! Use it with the OpenAI SDK.",
        base_url=f"{base_url}/v1",
    )


# ── Stats ──

@router.get("/api/key-stats")
async def key_stats():
    """Return active key count + current per-key limits for the landing page."""
    return {
        "active_keys": key_mgr.get_active_count(),
        "limits": key_mgr.get_per_key_limits(),
    }


# ── Student Usage ──

@router.get("/my/{key}")
async def student_usage_page(key: str):
    api_key = key_mgr.get_by_key(key)
    if not api_key:
        raise HTTPException(404, "Key not found")
    stats = key_mgr.get_usage_stats(key)
    return {
        "student_name": api_key.student_name,
        "active": api_key.active,
        "created_at": api_key.created_at,
        "usage_today": stats,
        "limits": {
            "chat": api_key.daily_chat,
            "image": api_key.daily_image,
            "tts": api_key.daily_tts,
            "vision": api_key.daily_vision,
            "video": api_key.daily_video,
            "podcast": api_key.daily_podcast,
            "web": api_key.daily_web,
            "rpm": api_key.rpm,
        },
    }


# ── Admin ──

def _check_admin(secret: str):
    if secret != ADMIN_SECRET:
        raise HTTPException(403, "Invalid admin secret")

@router.get("/admin/api/keys")
async def admin_list_keys(secret: str = ""):
    _check_admin(secret)
    keys = key_mgr.get_all_keys()
    usage_today = key_mgr.get_all_usage_today()
    usage_map = {u["key"]: u for u in usage_today}

    result = []
    for k in keys:
        u = usage_map.get(k.key, {})
        result.append({
            "student_name": k.student_name,
            "key": k.key,
            "key_short": k.key[:10] + "...",
            "active": k.active,
            "created_at": k.created_at,
            "total_calls_today": u.get("total_calls", 0),
            "errors_today": u.get("errors", 0),
        })
    return {"keys": result, "total": len(result)}

@router.post("/admin/api/keys/{key}/deactivate")
async def admin_deactivate(key: str, secret: str = ""):
    _check_admin(secret)
    key_mgr.deactivate(key)
    return {"success": True}

@router.post("/admin/api/keys/{key}/activate")
async def admin_activate(key: str, secret: str = ""):
    _check_admin(secret)
    key_mgr.activate(key)
    return {"success": True}


# ── Landing Page ──

@router.get("/", response_class=HTMLResponse)
async def landing_page(request: Request):
    base_url = str(request.base_url).rstrip("/")
    return LANDING_HTML.replace("{{BASE_URL}}", base_url)


LANDING_HTML = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GemGate — 免費 AI API 閘道</title>
<style>
  :root {
    --bg: #0f172a; --surface: #1e293b; --border: #334155;
    --text: #e2e8f0; --muted: #94a3b8; --accent: #38bdf8;
    --green: #4ade80; --red: #f87171; --yellow: #fbbf24;
    --font: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    --mono: 'JetBrains Mono', 'Fira Code', monospace;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: var(--font); background: var(--bg); color: var(--text); min-height: 100vh; }

  .container { max-width: 800px; margin: 0 auto; padding: 2rem 1.5rem; }

  /* Header */
  .hero { text-align: center; padding: 3rem 0 2rem; }
  .hero h1 { font-size: 2.5rem; font-weight: 800; letter-spacing: -0.02em; }
  .hero h1 span { color: var(--accent); }
  .hero p { color: var(--muted); font-size: 1.1rem; margin-top: 0.5rem; }
  .badges { display: flex; gap: 0.5rem; justify-content: center; margin-top: 1rem; flex-wrap: wrap; }
  .badge { background: var(--surface); border: 1px solid var(--border); border-radius: 999px;
           padding: 0.25rem 0.75rem; font-size: 0.8rem; color: var(--muted); }
  .badge.green { border-color: var(--green); color: var(--green); }

  /* Registration Card */
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
          padding: 1.5rem; margin: 1.5rem 0; }
  .card h2 { font-size: 1.2rem; margin-bottom: 1rem; }
  .input-row { display: flex; gap: 0.75rem; }
  .input-row input { flex: 1; background: var(--bg); border: 1px solid var(--border);
                     border-radius: 8px; padding: 0.75rem 1rem; color: var(--text);
                     font-size: 1rem; outline: none; }
  .input-row input:focus { border-color: var(--accent); }
  .input-row button { background: var(--accent); color: var(--bg); border: none;
                      border-radius: 8px; padding: 0.75rem 1.5rem; font-weight: 600;
                      font-size: 1rem; cursor: pointer; white-space: nowrap; }
  .input-row button:hover { opacity: 0.9; }
  .input-row button:disabled { opacity: 0.5; cursor: not-allowed; }

  /* Result box */
  .result { display: none; margin-top: 1rem; }
  .result.show { display: block; }
  .key-display { background: var(--bg); border: 1px solid var(--green); border-radius: 8px;
                 padding: 1rem; font-family: var(--mono); font-size: 0.95rem;
                 word-break: break-all; position: relative; }
  .key-display .copy-btn { position: absolute; top: 0.5rem; right: 0.5rem;
                           background: var(--surface); border: 1px solid var(--border);
                           border-radius: 6px; padding: 0.25rem 0.6rem; color: var(--muted);
                           cursor: pointer; font-size: 0.8rem; }
  .key-display .copy-btn:hover { color: var(--accent); border-color: var(--accent); }
  .success-msg { color: var(--green); font-size: 0.9rem; margin-top: 0.5rem; }

  /* API Docs */
  .endpoint { background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
              padding: 1rem; margin: 0.75rem 0; }
  .endpoint .method { display: inline-block; background: var(--green); color: var(--bg);
                      font-weight: 700; font-size: 0.75rem; padding: 0.15rem 0.5rem;
                      border-radius: 4px; margin-right: 0.5rem; }
  .endpoint .method.post { background: var(--accent); }
  .endpoint .path { font-family: var(--mono); font-size: 0.9rem; }
  .endpoint .desc { color: var(--muted); font-size: 0.85rem; margin-top: 0.3rem; }

  /* Code block */
  .code-block { background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
                padding: 1rem; font-family: var(--mono); font-size: 0.8rem;
                overflow-x: auto; white-space: pre; line-height: 1.6; color: var(--muted); }
  .code-block .kw { color: var(--accent); }
  .code-block .str { color: var(--green); }
  .code-block .comment { color: #64748b; }

  /* Tabs */
  .tabs { display: flex; gap: 0; border-bottom: 1px solid var(--border); margin-bottom: 1rem; }
  .tab { padding: 0.5rem 1rem; cursor: pointer; color: var(--muted); border-bottom: 2px solid transparent;
         font-size: 0.9rem; }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .tab-content { display: none; }
  .tab-content.active { display: block; }

  /* Quota table */
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th, td { padding: 0.5rem 0.75rem; text-align: left; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 500; }

  /* Footer */
  .footer { text-align: center; color: var(--muted); font-size: 0.8rem; padding: 2rem 0; }
  .footer a { color: var(--accent); text-decoration: none; }

  @media (max-width: 600px) {
    .hero h1 { font-size: 1.8rem; }
    .input-row { flex-direction: column; }
    .container { padding: 1rem; }
  }
</style>
</head>
<body>
<div class="container">

  <!-- Hero -->
  <div class="hero">
    <h1>⚡ <span>GemGate</span></h1>
    <p>免費 AI API 閘道 — 零成本、零 API Key 費用</p>
    <div class="badges">
      <span class="badge green">✓ AI 對話</span>
      <span class="badge green">✓ 圖片生成</span>
      <span class="badge green">✓ 圖片理解</span>
      <span class="badge green">✓ 語音合成</span>
      <span class="badge">✓ 影片生成</span>
      <span class="badge">✓ Podcast</span>
    </div>
  </div>

  <!-- Registration -->
  <div class="card" id="register-card">
    <h2 style="display:flex; justify-content:space-between; align-items:center;">
      🔑 取得你的 API Key
      <span id="active-count" style="font-size:0.8rem; font-weight:400; color:var(--muted);
            background:var(--bg); padding:0.2rem 0.7rem; border-radius:999px; border:1px solid var(--border);">
        載入中...
      </span>
    </h2>
    <p style="color:var(--muted); font-size:0.9rem; margin-bottom:1rem;">
      輸入你的姓名或學號，即可取得免費 API Key。完全相容 OpenAI SDK。
      <br><span style="color:var(--yellow); font-size:0.8rem;">Key 有效期 24 小時，過期後重新申請即可。</span>
    </p>
    <div class="input-row">
      <input type="text" id="student-name" placeholder="輸入姓名或學號" maxlength="50" />
      <button id="register-btn" onclick="doRegister()">取得 Key</button>
    </div>
    <div class="result" id="result">
      <div class="key-display">
        <span id="api-key-text"></span>
        <button class="copy-btn" onclick="copyKey()">Copy</button>
      </div>
      <div class="success-msg" id="result-msg"></div>
    </div>
  </div>

  <!-- Quick Start -->
  <div class="card">
    <h2>🚀 快速開始</h2>
    <div class="tabs">
      <div class="tab active" onclick="switchTab('gas')">Google Apps Script</div>
      <div class="tab" onclick="switchTab('python')">Python</div>
      <div class="tab" onclick="switchTab('curl')">cURL</div>
    </div>

    <div class="tab-content active" id="tab-gas">
      <div class="code-block"><span class="kw">function</span> askAI(prompt) {
  <span class="kw">const</span> url = <span class="str">"{{BASE_URL}}/v1/chat/completions"</span>;
  <span class="kw">const</span> options = {
    method: <span class="str">"post"</span>,
    headers: {
      <span class="str">"Authorization"</span>: <span class="str">"Bearer YOUR_API_KEY"</span>,
      <span class="str">"Content-Type"</span>: <span class="str">"application/json"</span>
    },
    payload: JSON.stringify({
      model: <span class="str">"gemini"</span>,
      messages: [{ role: <span class="str">"user"</span>, content: prompt }]
    })
  };
  <span class="kw">const</span> res = UrlFetchApp.fetch(url, options);
  <span class="kw">const</span> data = JSON.parse(res.getContentText());
  <span class="kw">return</span> data.choices[0].message.content;
}</div>
    </div>

    <div class="tab-content" id="tab-python">
      <div class="code-block"><span class="comment"># pip install openai</span>
<span class="kw">from</span> openai <span class="kw">import</span> OpenAI

client = OpenAI(
    api_key=<span class="str">"YOUR_API_KEY"</span>,
    base_url=<span class="str">"{{BASE_URL}}/v1"</span>
)

<span class="comment"># Chat</span>
resp = client.chat.completions.create(
    model=<span class="str">"gemini"</span>,
    messages=[{<span class="str">"role"</span>: <span class="str">"user"</span>, <span class="str">"content"</span>: <span class="str">"Hello!"</span>}]
)
print(resp.choices[0].message.content)

<span class="comment"># Image</span>
img = client.images.generate(
    model=<span class="str">"gemini-image"</span>,
    prompt=<span class="str">"A cute cat with sunglasses"</span>
)
print(img.data[0].b64_json[:50])</div>
    </div>

    <div class="tab-content" id="tab-curl">
      <div class="code-block"><span class="comment"># Chat</span>
curl {{BASE_URL}}/v1/chat/completions \
  -H <span class="str">"Authorization: Bearer YOUR_API_KEY"</span> \
  -H <span class="str">"Content-Type: application/json"</span> \
  -d <span class="str">'{"model":"gemini","messages":[{"role":"user","content":"Hello!"}]}'</span>

<span class="comment"># Image</span>
curl {{BASE_URL}}/v1/images/generations \
  -H <span class="str">"Authorization: Bearer YOUR_API_KEY"</span> \
  -H <span class="str">"Content-Type: application/json"</span> \
  -d <span class="str">'{"model":"gemini-image","prompt":"A sunset over mountains"}'</span></div>
    </div>
  </div>

  <!-- API Endpoints -->
  <div class="card">
    <h2>📡 API 端點</h2>
    <div class="endpoint">
      <span class="method">GET</span>
      <span class="path">/v1/models</span>
      <div class="desc">列出可用模型</div>
    </div>
    <div class="endpoint">
      <span class="method post">POST</span>
      <span class="path">/v1/chat/completions</span>
      <div class="desc">AI 對話（支援附圖 Vision）</div>
    </div>
    <div class="endpoint">
      <span class="method post">POST</span>
      <span class="path">/v1/images/generations</span>
      <div class="desc">文字生成圖片</div>
    </div>
    <div class="endpoint">
      <span class="method post">POST</span>
      <span class="path">/v1/audio/speech</span>
      <div class="desc">文字轉語音（回傳 MP3）</div>
    </div>
    <div class="endpoint">
      <span class="method">GET</span>
      <span class="path">/v1/usage</span>
      <div class="desc">查詢今日使用量</div>
    </div>
  </div>

  <!-- Quotas -->
  <div class="card">
    <h2>📊 每日限額（每把 Key）</h2>
    <p style="color:var(--yellow); font-size:0.8rem; margin-bottom:0.75rem;">
      限額依 Google 帳號全局額度自動均分，使用人數越多每人分配越少。
    </p>
    <table>
      <tr><th>功能</th><th>Google 免費帳號全域上限</th><th>你的每日上限</th><th>RPM</th></tr>
      <tr><td>AI 對話</td><td>~500</td><td id="lim-chat">-</td><td>5</td></tr>
      <tr><td>圖片生成</td><td>~100</td><td id="lim-image">-</td><td>5</td></tr>
      <tr><td>語音合成</td><td>無限制 (本地)</td><td id="lim-tts">-</td><td>5</td></tr>
      <tr><td>圖片理解</td><td>~10</td><td id="lim-vision">-</td><td>5</td></tr>
      <tr><td>影片生成</td><td>~10</td><td id="lim-video">-</td><td>5</td></tr>
      <tr><td>Podcast</td><td>~10</td><td id="lim-podcast">-</td><td>5</td></tr>
    </table>
    <p style="color:var(--muted); font-size:0.75rem; margin-top:0.5rem;">
      實際可用量取決於 Google 政策，可能隨時變動。以上為觀察值，非官方保證。
    </p>
  </div>

  <div class="footer">
    由 <a href="https://github.com/ai-cooperation/gemgate">GemGate</a> 驅動
    — 自架 Google AI 閘道，零成本提供多模態 AI 能力
  </div>

</div>

<script>
async function doRegister() {
  const name = document.getElementById('student-name').value.trim();
  if (!name) { alert('請輸入你的姓名或學號'); return; }

  const btn = document.getElementById('register-btn');
  btn.disabled = true; btn.textContent = '...';

  try {
    const res = await fetch('/register', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({student_name: name})
    });
    const data = await res.json();
    if (data.success) {
      document.getElementById('api-key-text').textContent = data.api_key;
      document.getElementById('result-msg').textContent =
        `歡迎 ${data.student_name}！Base URL: ${data.base_url}`;
      document.getElementById('result').classList.add('show');
      // Replace placeholder in code samples
      document.querySelectorAll('.code-block').forEach(el => {
        el.innerHTML = el.innerHTML.replace(/YOUR_API_KEY/g, data.api_key);
      });
    } else {
      alert(data.message || 'Registration failed');
    }
  } catch(e) { alert('Error: ' + e.message); }
  finally { btn.disabled = false; btn.textContent = '取得 Key'; }
}

function copyKey() {
  const key = document.getElementById('api-key-text').textContent;
  // Fallback for HTTP (navigator.clipboard requires HTTPS)
  const ta = document.createElement('textarea');
  ta.value = key;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  document.execCommand('copy');
  document.body.removeChild(ta);
  const btn = event.target;
  btn.textContent = 'Copied!';
  setTimeout(() => btn.textContent = 'Copy', 1500);
}

// Load active key count + limits
fetch('/api/key-stats').then(r => r.json()).then(d => {
  document.getElementById('active-count').textContent = d.active_keys + ' 把 Key 使用中';
  if (d.limits) {
    for (const [ep, val] of Object.entries(d.limits)) {
      const el = document.getElementById('lim-' + ep);
      if (el) el.textContent = val + ' 次';
    }
  }
}).catch(() => {
  document.getElementById('active-count').textContent = '';
});

function switchTab(id) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('tab-' + id).classList.add('active');
}
</script>
</body>
</html>"""
