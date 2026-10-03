"""
The setup form a member uses to connect their own Walden login (issue #240).

Served to Telegram as a Mini App: the bot's "Connect Walden account" button
opens GET /onboarding/walden inside Telegram, and the form posts back to the
same path. See app/services/member_setup.py for the flow and what is checked.

Two choices here are about the password, and are deliberate:

* **No third-party script on the page.** Telegram's own telegram-web-app.js is
  not loaded. Telegram puts the signed init data in the URL fragment
  (``#tgWebAppData=...``), which the page reads itself. A strict CSP with a
  per-response nonce allows only this page's own inline script and style.
* **The body is read by hand, not by a pydantic model.** FastAPI's automatic
  422 response echoes the offending input back, which here would be the
  password. Every response carries a message written in member_setup instead.
"""

import json
import logging
import secrets

from fastapi import APIRouter, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.providers.telegram_provider import validate_init_data
from app.services.member_setup import SETUP_PATH, SubmitStatus, submit_login

logger = logging.getLogger(__name__)

router = APIRouter(tags=["onboarding"])

_NO_STORE = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}

_STATUS_CODES = {
    SubmitStatus.SAVED: 200,
    SubmitStatus.REJECTED: 200,
    SubmitStatus.UNKNOWN: 200,
    SubmitStatus.INVALID: 400,
    SubmitStatus.RATE_LIMITED: 429,
    SubmitStatus.FORBIDDEN: 403,
}

_REPO_URL = "https://github.com/alexenos/teetime/blob/main/app/services/member_setup.py"


def _page(nonce: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect Walden</title>
<style nonce="{nonce}">
  :root {{ --bg:#ffffff; --fg:#1c1c1e; --muted:#6b6b70; --accent:#2481cc; --accent-fg:#ffffff;
          --field:#f2f2f7; --bad:#c0392b; --good:#1e8e3e; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#17212b; --fg:#f5f5f5; --muted:#a0a8b0; --field:#232e3c; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:20px 16px 32px; background:var(--bg); color:var(--fg);
         font:16px/1.45 -apple-system, system-ui, "Segoe UI", Roboto, sans-serif; }}
  h1 {{ font-size:20px; margin:0 0 8px; }}
  p, li {{ color:var(--muted); font-size:14px; }}
  ul {{ padding-left:18px; margin:8px 0 16px; }}
  label {{ display:block; font-size:14px; margin:14px 0 6px; }}
  input[type=text], input[type=password] {{ width:100%; padding:12px; font-size:16px;
         border:1px solid transparent; border-radius:10px; background:var(--field); color:var(--fg); }}
  .consent {{ display:flex; gap:10px; align-items:flex-start; margin:18px 0; }}
  .consent input {{ margin-top:3px; width:18px; height:18px; flex:none; }}
  .consent span {{ font-size:14px; color:var(--fg); }}
  button {{ width:100%; padding:14px; font-size:16px; font-weight:600; border:0;
           border-radius:10px; background:var(--accent); color:var(--accent-fg); }}
  button:disabled {{ opacity:.6; }}
  #result {{ margin-top:16px; font-size:15px; min-height:1.5em; }}
  #result.bad {{ color:var(--bad); }} #result.good {{ color:var(--good); }}
  a {{ color:var(--accent); }}
</style>
</head>
<body>
<h1>Connect your Walden account</h1>
<p>The tee time bot books under your own Walden membership, so it needs your Walden login.</p>
<ul>
  <li>It goes straight from your phone to the booking service, encrypted. It is never a
      chat message, and the person who runs the bot never sees it.</li>
  <li>Walden checks it before it's saved.</li>
  <li>The bot's admin can ask the bot to book on your behalf, under your membership.</li>
  <li>Send <b>/forget</b> to the bot any time to delete it.
      <a href="{_REPO_URL}" target="_blank" rel="noopener">The code that handles it</a>
      is public.</li>
</ul>
<form id="form" autocomplete="on" novalidate>
  <label for="login">Walden member number</label>
  <input id="login" name="login" type="text" autocomplete="username" autocapitalize="none"
         spellcheck="false" maxlength="128" required>
  <label for="password">Walden password</label>
  <input id="password" name="password" type="password" autocomplete="current-password"
         maxlength="256" required>
  <label class="consent"><input id="consent" type="checkbox">
    <span>I agree that the bot may log in to Walden as me to book the tee times I ask for.</span>
  </label>
  <button id="submit" type="submit">Connect</button>
  <div id="result" role="status" aria-live="polite"></div>
</form>
<script nonce="{nonce}">
(function () {{
  var hash = new URLSearchParams(window.location.hash.slice(1));
  var initData = hash.get("tgWebAppData") || "";
  try {{
    var theme = JSON.parse(hash.get("tgWebAppThemeParams") || "{{}}");
    var map = {{bg_color:"--bg", text_color:"--fg", hint_color:"--muted", button_color:"--accent",
               button_text_color:"--accent-fg", secondary_bg_color:"--field"}};
    Object.keys(map).forEach(function (k) {{
      if (/^#[0-9a-fA-F]{{3,8}}$/.test(theme[k] || "")) {{
        document.documentElement.style.setProperty(map[k], theme[k]);
      }}
    }});
  }} catch (e) {{}}
  // Drop the signed data from the address bar once read.
  history.replaceState(null, "", window.location.pathname);

  var form = document.getElementById("form");
  var result = document.getElementById("result");
  var button = document.getElementById("submit");
  function show(text, cls) {{ result.textContent = text; result.className = cls || ""; }}
  function close() {{
    try {{
      if (window.TelegramWebviewProxy) {{ window.TelegramWebviewProxy.postEvent("web_app_close", "{{}}"); }}
      else if (window.parent !== window) {{
        window.parent.postMessage(JSON.stringify({{eventType:"web_app_close", eventData:{{}}}}), "https://web.telegram.org");
      }}
    }} catch (e) {{}}
  }}
  if (!initData) {{
    show("Open this from the Connect Walden account button in your chat with the bot.", "bad");
    button.disabled = true;
  }}
  form.addEventListener("submit", function (ev) {{
    ev.preventDefault();
    button.disabled = true;
    show("Checking with Walden…");
    fetch("{SETUP_PATH}", {{
      method: "POST",
      headers: {{"Content-Type": "application/json", "X-Telegram-Init-Data": initData}},
      body: JSON.stringify({{
        login: document.getElementById("login").value,
        password: document.getElementById("password").value,
        consent: document.getElementById("consent").checked
      }}),
      credentials: "omit",
      cache: "no-store"
    }}).then(function (r) {{ return r.json(); }}).then(function (body) {{
      var ok = body.status === "saved";
      show(body.message || "Something went wrong.", ok ? "good" : "bad");
      if (ok) {{
        document.getElementById("password").value = "";
        setTimeout(close, 1800);
      }} else {{
        button.disabled = false;
      }}
    }}).catch(function () {{
      show("Couldn't reach the booking service. Please try again.", "bad");
      button.disabled = false;
    }});
  }});
}})();
</script>
</body>
</html>
"""


@router.get(SETUP_PATH, response_class=HTMLResponse, include_in_schema=False)
async def setup_page() -> HTMLResponse:
    """The setup form. Holds no member data: who is asking arrives with the submission."""
    nonce = secrets.token_urlsafe(16)
    csp = (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
        "connect-src 'self'; img-src 'self'; form-action 'self'; base-uri 'none'; "
        # Telegram Web shows Mini Apps in an iframe; the mobile apps use a webview.
        "frame-ancestors https://web.telegram.org"
    )
    return HTMLResponse(_page(nonce), headers={**_NO_STORE, "Content-Security-Policy": csp})


def _reply(status: SubmitStatus, message: str) -> JSONResponse:
    return JSONResponse(
        {"status": status.value, "message": message},
        status_code=_STATUS_CODES[status],
        headers=_NO_STORE,
    )


@router.post(SETUP_PATH, include_in_schema=False)
async def submit_setup(
    request: Request,
    x_telegram_init_data: str = Header("", alias="X-Telegram-Init-Data"),
) -> JSONResponse:
    """Check and save a member's Walden login. See member_setup.submit_login."""
    user = validate_init_data(x_telegram_init_data)
    if user is None:
        return _reply(
            SubmitStatus.FORBIDDEN,
            "This form has to be opened from the bot's button in Telegram. Close it and tap "
            "the button again.",
        )

    try:
        body = json.loads(await request.body())
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return _reply(SubmitStatus.INVALID, "The form sent something unexpected. Try again.")
    login, password, consent = body.get("login"), body.get("password"), body.get("consent")
    if not isinstance(login, str) or not isinstance(password, str):
        return _reply(SubmitStatus.INVALID, "Enter both your Walden login and password.")

    try:
        result = await submit_login(user, login, password, consent is True)
    except Exception:
        # The traceback names no locals, so it carries neither value; still,
        # the message is ours rather than the exception's.
        logger.exception(f"Setup form failed for Telegram user {user.get('id')}")
        return _reply(
            SubmitStatus.UNKNOWN, "Something went wrong on our side, so nothing was saved."
        )
    return _reply(result.status, result.message)
