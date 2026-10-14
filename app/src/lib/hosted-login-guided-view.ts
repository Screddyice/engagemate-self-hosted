/** Isolated customer controls for a private Instagram browser. No DOM or image is relayed. */
export function hostedLoginDocument(id: string, nonce: string, expiresAt: string, username: string): string {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>Connect Instagram | EngageMate</title>
<style nonce="${nonce}">
:root{color-scheme:dark;--bg:#09090b;--surface:#0c0c0f;--field:#111114;--text:#fafafa;--muted:#a1a1aa;--border:#27272a;--border-strong:#3f3f46;--soft:#18181b;--button:#fafafa;--button-text:#09090b;--ring:#a78bfa;--success:#4ade80;--success-bg:#052e1b}
:root[data-theme="light"]{color-scheme:light;--bg:#fff;--surface:#fff;--field:#fff;--text:#0d1b3e;--muted:#6b7280;--border:#e4e4e7;--border-strong:#a1a1aa;--soft:#f4f4f5;--button:#0d1b3e;--button-text:#fff;--ring:#7c3aed;--success:#15803d;--success-bg:#dcfce7}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}button,input{font:inherit}button{cursor:pointer}button:disabled{opacity:.5;cursor:wait}button:focus-visible,input:focus-visible,a:focus-visible{outline:2px solid var(--ring);outline-offset:2px}a{color:inherit}p{margin:0}.muted{color:var(--muted)}.hint{font-size:12px;line-height:1.55}
.site-header{border-bottom:1px solid var(--border)}.site-header-inner{max-width:1100px;min-height:64px;margin:auto;padding:0 32px;display:flex;align-items:center;justify-content:space-between;gap:16px}.brand{font-size:18px;font-weight:700;letter-spacing:-.025em;text-decoration:none}.header-label{font-size:12px;color:var(--muted)}
main{max-width:660px;margin:0 auto;padding:44px 24px 72px}.eyebrow{margin-bottom:16px;color:var(--muted);font-size:12px;font-weight:600;letter-spacing:.08em;text-transform:uppercase}h1{font-size:28px;line-height:1.2;letter-spacing:-.035em;margin:0 0 10px}h2{font-size:17px;line-height:1.3;letter-spacing:-.02em;margin:0}.intro{max-width:58ch;color:var(--muted);line-height:1.65}
.connection-card{margin-top:30px;background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}.card-head{padding:22px 24px;border-bottom:1px solid var(--border)}.card-head .eyebrow{margin-bottom:7px}.status-row{display:flex;align-items:flex-start;gap:10px}.status-mark{width:8px;height:8px;flex:none;margin-top:7px;background:#8b5cf6;border-radius:50%}#status{min-height:22px;font-weight:500}#remaining{margin:5px 0 0 18px;font-size:12px;font-variant-numeric:tabular-nums}.card-body{padding:24px}.section-head{margin-bottom:20px}.section-head p{margin-top:5px;color:var(--muted)}
form{display:grid;gap:16px}label{display:grid;gap:6px;font-weight:500}input{width:100%;min-width:0;min-height:42px;padding:9px 12px;border:1px solid var(--border-strong);border-radius:6px;background:var(--field);color:var(--text)}input:hover{border-color:var(--ring)}.primary,.secondary,.choice{min-height:40px;padding:9px 16px;border-radius:6px;font-weight:500}.primary{border:1px solid transparent;background:linear-gradient(90deg,#7c3aed,#4f46e5);color:#fff;box-shadow:0 1px 3px rgba(0,0,0,.14)}.primary:hover:not(:disabled){background:linear-gradient(90deg,#8b5cf6,#6366f1)}.secondary,.choice{border:1px solid var(--border);background:var(--surface);color:var(--text)}.secondary:hover:not(:disabled),.choice:hover:not(:disabled){background:var(--soft)}form .primary{justify-self:start;margin-top:2px}.privacy-note{margin-top:0}
.panel{margin-top:20px;padding:18px;border:1px solid var(--border);border-radius:8px;background:var(--soft)}.panel strong{display:block;font-weight:500}.choices{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}.choices:empty{display:none}.choice{background:var(--surface)}.actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:18px 24px;border-top:1px solid var(--border)}.actions .cancel{margin-left:auto;border:0;background:transparent;color:var(--muted);padding:9px 0}.actions .cancel:hover{color:var(--text)}
#done{padding:16px 18px;margin-bottom:20px;background:var(--success-bg);color:var(--success);border-radius:8px;font-weight:500}#return{display:inline-block;margin-top:16px;text-underline-offset:3px;font-weight:500}[hidden]{display:none!important}
@media(max-width:600px){.site-header-inner{min-height:56px;padding:0 20px}.header-label{display:none}main{padding:30px 16px 48px}h1{font-size:25px}.connection-card{margin-top:24px}.card-head,.card-body{padding:20px}.actions{padding:16px 20px}.actions .cancel{margin-left:0}.actions button{width:100%}form .primary{width:100%}}
</style></head><body><header class="site-header"><div class="site-header-inner"><a class="brand" href="/dashboard">EngageMate</a><span class="header-label">Instagram connection</span></div></header><main>
<p class="eyebrow">Accounts / Connect Instagram</p><h1>Connect your Instagram account</h1><p class="intro">Sign in and complete any Instagram verification here. EngageMate uses a private temporary browser for the connection and closes it after 10 minutes.</p>
<div class="connection-card"><div class="card-head"><p class="eyebrow">Connection status</p><div class="status-row"><span class="status-mark" aria-hidden="true"></span><p id="status" role="status" aria-live="polite">Opening Instagram…</p></div><p id="remaining" class="muted"></p></div>
<div class="card-body"><div id="done" hidden>Instagram is connected. Continuing your setup…</div><section id="controls">
<form id="login" autocomplete="off" hidden><div class="section-head"><h2>Sign in to Instagram</h2><p>Use the account you selected in EngageMate.</p></div><label for="username">Instagram username<input id="username" name="username" autocomplete="username" autocapitalize="none" spellcheck="false" maxlength="30" required></label><label for="password">Instagram password<input id="password" name="password" type="password" autocomplete="off" maxlength="1024" required></label><button class="primary" type="submit">Log in to Instagram</button><p class="hint muted privacy-note">EngageMate clears the password field after submission. It does not save your password.</p></form>
<form id="code-form" autocomplete="off" hidden><div class="section-head"><h2 id="code-title">Verify your account</h2><p id="code-instructions"></p></div><label for="code">Instagram security code<input id="code" aria-describedby="code-instructions" name="code" autocomplete="one-time-code" autocapitalize="none" spellcheck="false" maxlength="20" required></label><button class="primary" type="submit">Submit code</button><p class="hint muted privacy-note">EngageMate clears this field after you submit the code.</p><p class="hint muted">No code yet? Delivery can take a few minutes. Use Send another code or Try another way below if Instagram offers them. Authenticator codes appear in your authenticator app; they are not sent to you. EngageMate cannot confirm delivery.</p></form>
<div id="options-panel" class="panel" hidden><strong id="step-prompt"></strong><div id="choices" class="choices"></div></div>
</section><a href="/accounts/connect/instagram" id="return" hidden>Return to EngageMate</a></div><div class="actions" id="actions"><button id="check" class="secondary" type="button">Check again</button><button id="finish" class="primary" type="button" hidden>Finish connecting</button><button id="cancel" class="cancel" type="button">Cancel connection</button></div></div>
<script nonce="${nonce}">${viewerScript(id, expiresAt, username)}</script></main></body></html>`;
}
function viewerScript(id: string, expiresAt: string, username: string): string {
  // Values come from validated service output, never from query strings.
  return `"use strict";
(function () {
  try { const stored = localStorage.getItem("em-theme"); document.documentElement.dataset.theme = stored === "light" || stored === "dark" ? stored : (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"); }
  catch { document.documentElement.dataset.theme = "dark"; }
  const base = ${JSON.stringify(`/api/instagram/connect/${id}`)}, expires = ${JSON.stringify(expiresAt)}, expectedUsername = ${JSON.stringify(username)};
  const byId = (id) => document.getElementById(id), status = byId("status"), login = byId("login"), codeForm = byId("code-form"), usernameField = byId("username"), passwordField = byId("password"), codeField = byId("code"), panel = byId("options-panel"), choices = byId("choices"), prompt = byId("step-prompt");
  usernameField.value = expectedUsername;
  let ended = false, busy = false, currentKind = "", lastStatus = 0, lastChannel = "", automaticStopped = false, alternativeSelected = false;
  const automaticActions = new Set();
  const labels = {send_code:"Send code",continue_verification:"Continue verification",resend_code:"Send another code",try_another_way:"Try another way",use_sms:"Text message",use_whatsapp:"WhatsApp",use_authenticator:"Authenticator app",use_email:"Email",use_backup_code:"Backup code",confirm_login:"Confirm this login"};
  const errors = {session_unverified:"Instagram has not provided a complete signed-in session yet. Choose Check again to see whether another verification step is required.",identity_check_unavailable:"EngageMate could not verify the signed-in account with Instagram. Your connection is still open. Wait a moment, then choose Finish connecting again.",sign_in_required:"Instagram still needs a sign-in step. Check the instructions here and try again.",identity_mismatch:"The signed-in Instagram account does not match the selected username.",invalid_input:"Check the value you entered and try again.",session_expired:"This temporary connection expired. Return to EngageMate and start again.",session_not_found:"This temporary connection is no longer available.",auth_required:"Your EngageMate session expired. Sign in to EngageMate again.",connection_unavailable:"The connection was interrupted. Check the current step before trying again.",slow_down:"Please wait a moment before trying again.",instagram_rate_limited:"Instagram is limiting sign-in requests. Wait before trying again."};
  function message(code) { return errors[code] || errors.connection_unavailable; }
  function lock() { document.querySelectorAll("#controls button,#controls input,#actions button").forEach((element) => { element.disabled = busy || ended; }); }
  function end(copy, success) { ended = true; byId("remaining").hidden = true; passwordField.value = ""; codeField.value = ""; byId("controls").hidden = true; byId("actions").hidden = true; byId("done").hidden = !success; byId("return").hidden = false; status.textContent = copy; lock(); }
  async function api(suffix, method, body) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), suffix === "/complete" ? 50000 : 20000);
    try {
      const response = await fetch(base + suffix, {method:method || "GET",credentials:"same-origin",cache:"no-store",signal:controller.signal,headers:body ? {"Content-Type":"application/json"} : {},body:body ? JSON.stringify(body) : undefined});
      if (response.status === 204) return {};
      if (!response.ok) { const data = await response.json().catch(() => ({})); if ([401,404,410].includes(response.status)) end(message(data.code), false); throw new Error(data.code || "connection_unavailable"); }
      return await response.json();
    } finally { clearTimeout(timer); }
  }
  const codeHelp = {
    unknown:["Find your Instagram security code", "Instagram has not identified where to get this code. Check your authenticator app and recent messages from Instagram. If you cannot find it, use Try another way when available."],
    authenticator:["Open your authenticator app", "Find the Instagram entry in the authenticator app you set up for this account, such as Google Authenticator, Microsoft Authenticator or Authy. Enter the current code below before it changes."],
    sms:["Check your text messages", "Open the Messages app on the phone linked to your Instagram account. Look for a recent text message from Instagram and enter its security code below."],
    whatsapp:["Open WhatsApp on your phone", "Use WhatsApp for the phone number linked to your Instagram account. Look for a recent message from Instagram and enter its security code below."],
    email:["Check your email inbox", "Open the email account linked to Instagram. Look for the latest security-code email from Instagram, including in Spam or Junk, and enter the code below."],
    backup:["Find your saved Instagram backup codes", "Use one of the unused backup codes you saved when you set up two-factor authentication for Instagram. Enter that code below."]
  };
  const progress = {
    sign_in:["Submitting your username and password to Instagram…", "Password submitted. Waiting for Instagram’s next step…"],
    submit_code:["Submitting your security code to Instagram…", "Code submitted. Waiting for Instagram to verify it…"],
    send_code:["Requesting a security code from Instagram…", "Code requested. Waiting for Instagram to show the destination…"],
    continue_verification:["Confirming your verification method with Instagram…", "Verification method submitted. Waiting for Instagram’s next step…"],
    resend_code:["Requesting another security code from Instagram…", "Another code requested. Check the delivery instructions below."],
    choose_action:["Submitting your verification selection to Instagram…", "Verification selection submitted. Waiting for Instagram’s next step…"]
  };
  function render(step) {
    if (ended) return;
    login.hidden = step.kind !== "login"; codeForm.hidden = step.kind !== "code"; byId("finish").hidden = step.kind !== "ready";
    panel.hidden = !["method","approval","unsupported"].includes(step.kind) && !(step.options || []).length; choices.replaceChildren();
    for (const action of step.options || []) { if (!labels[action]) continue; const button = document.createElement("button"); button.type = "button"; button.className = "choice"; button.textContent = labels[action]; button.addEventListener("click", () => { automaticStopped = true; void perform({type:"choose_action",action}); }); choices.append(button); }
    if (step.kind === "code") {
      const help = codeHelp[step.channel] || codeHelp.unknown;
      byId("code-title").textContent = help[0]; byId("code-instructions").textContent = help[1];
      prompt.textContent = "Need another code or a different verification method?";
    }
    else if (step.kind === "approval") prompt.textContent = "Approve the login request in Instagram on your phone, then choose Check again.";
    else if (step.kind === "method") prompt.textContent = "Choose how to verify this login. If Instagram then shows Send code or Continue verification, select it to request the next step.";
    else if (step.kind === "unsupported") prompt.textContent = "Instagram requested a security step that EngageMate cannot present here. Complete it in Instagram, then choose Check again. You can cancel this connection at any time.";
    if (step.kind !== currentKind || (step.kind === "code" && step.channel !== lastChannel)) { currentKind = step.kind; lastChannel = step.channel || ""; codeField.value = "";
      if (step.kind === "login") status.textContent = "Enter your Instagram username and password.";
      if (step.kind === "code") status.textContent = "Instagram needs a security code. " + (codeHelp[step.channel] || codeHelp.unknown)[0] + ".";
      if (step.kind === "approval") status.textContent = "Waiting for your approval in Instagram.";
      if (step.kind === "method") status.textContent = "Choose a verification method.";
      if (step.kind === "ready") status.textContent = "Instagram sign-in appears complete. Choose Finish connecting to verify it.";
      if (step.kind === "unsupported") status.textContent = "This Instagram security step needs your attention.";
    }
    if (step.kind === "code" && step.invalid) status.textContent = "Instagram did not accept that code. Get the latest code using the instructions below and try again.";
    lock();
    preferAlternative(step);
  }
  function preferAlternative(step) {
    if (ended || busy || automaticStopped) return;
    const options = step.options || [];
    let action;
    if (step.kind === "code" && step.channel === "whatsapp" && !alternativeSelected) {
      action = ["use_sms","use_email","use_authenticator"].find((option) => options.includes(option));
      if (!action && options.includes("try_another_way")) action = "try_another_way";
    }
    if (step.kind === "method" && !alternativeSelected) action = ["use_sms","use_email","use_authenticator"].find((option) => options.includes(option));
    if (alternativeSelected && step.kind === "method") action = ["send_code","continue_verification"].find((option) => options.includes(option));
    if (!action || automaticActions.has(action)) return;
    automaticActions.add(action);
    if (["use_sms","use_email","use_authenticator"].includes(action)) alternativeSelected = true;
    void perform({type:"choose_action",action});
  }
  async function refresh() { if (ended || busy) return; try { render(await api("/step")); } catch (error) { if (!ended) status.textContent = message(error.message); } }
  async function perform(body) {
    if (ended || busy) return; busy = true; lock(); const copy = progress[body.type === "choose_action" ? body.action : body.type] || progress.choose_action; status.textContent = copy[0];
    try { await api("/input","POST",body); if (!ended) status.textContent = copy[1]; }
    catch (error) { automaticStopped = true; if (!ended) status.textContent = message(error.message); }
    finally { busy = false; lock(); }
    await refresh();
  }
  login.addEventListener("submit", (event) => { event.preventDefault(); const name = usernameField.value.trim().replace(/^@/, "").toLowerCase(), password = passwordField.value; passwordField.value = "";
    if (name !== expectedUsername) { status.textContent = "Use the Instagram username selected when you started this connection."; return; }
    if (password) void perform({type:"sign_in",username:name,password});
  });
  codeForm.addEventListener("submit", (event) => { event.preventDefault(); const code = codeField.value.trim(); codeField.value = ""; if (code) void perform({type:"submit_code",code}); });
  byId("check").addEventListener("click", () => void refresh());
  byId("finish").addEventListener("click", async () => { if (ended || busy) return; busy = true; lock(); status.textContent = "Verifying your signed-in account…";
    try { const state = await api("/complete","POST"); if (state.state === "connected") { connected(state); } else status.textContent = "Finish signing in to Instagram, then try again."; }
    catch (error) { if (!ended) status.textContent = message(error.message); } finally { busy = false; lock(); }
  });
  byId("cancel").addEventListener("click", async () => { if (ended || busy) return; busy = true; lock(); try { await api("","DELETE"); end("Connection cancelled.",false); } catch { if (!ended) status.textContent = "Cancellation was not confirmed. Try again."; } finally { busy = false; lock(); } });
  function connected() {
    end("Connection confirmed.",true);
    const next = byId("return");
    next.href = "/onboarding";
    next.textContent = "Continue setup";
    // The completion response sets the selected-account cookie before this
    // navigation. The server resumes the appropriate saved onboarding step.
    window.location.replace("/onboarding");
  }
  async function poll() { if (ended) return;
    byId("remaining").textContent = "Time remaining: " + Math.max(0, Math.ceil((Date.parse(expires)-Date.now())/60000)) + " minute(s)";
    try { if (Date.now()-lastStatus >= 5000) { const state = await api(""); lastStatus = Date.now(); if (state.state === "connected") { connected(state); return; } if (state.state === "error") { end(message(state.code),false); return; } } if (Date.now() >= Date.parse(expires)) { end(errors.session_expired,false); return; } await refresh(); }
    catch (error) { if (!ended) status.textContent = message(error.message); } finally { if (!ended) setTimeout(poll,2000); }
  }
  window.addEventListener("pagehide", () => { ended = true; passwordField.value = ""; codeField.value = ""; });
  void poll();
})();
`;
}
