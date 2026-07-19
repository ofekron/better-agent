from __future__ import annotations

HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>Line Switch</title>
<style nonce="__NONCE__">
:root{font-family:ui-sans-serif,system-ui,sans-serif;color:#f6f7fb;background:#080b12}
*{box-sizing:border-box}body{margin:0;min-height:100dvh;display:grid;place-items:center;padding:max(18px,env(safe-area-inset-top)) max(18px,env(safe-area-inset-right)) max(18px,env(safe-area-inset-bottom)) max(18px,env(safe-area-inset-left));background:radial-gradient(circle at top,#17213b 0,#080b12 55%)}
main{width:min(100%,480px);padding:clamp(22px,6vw,38px);border:1px solid #29334b;border-radius:24px;background:#111725e8;box-shadow:0 24px 70px #0009;transition:transform .25s ease,border-color .25s ease}
h1{margin:0 0 6px;font-size:clamp(28px,8vw,42px);letter-spacing:-.04em}.sub{margin:0 0 28px;color:#9ca8c1}.status{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:20px;padding:14px 16px;border-radius:14px;background:#090d17}.label{color:#9ca8c1}.active{font-weight:750;font-size:20px}.lines{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
button,input{font:inherit}button{min-height:52px;border:1px solid #35415d;border-radius:13px;color:#fff;background:#1b2438;font-weight:700;cursor:pointer;transition:transform .18s ease,background .18s ease,border-color .18s ease,opacity .18s ease}button:hover:not(:disabled){background:#273452;border-color:#6177a6;transform:translateY(-2px)}button:active:not(:disabled){transform:translateY(0)}button.current{background:#235c47;border-color:#3ba779}button:disabled{cursor:not-allowed;opacity:.48}.message{min-height:42px;margin-top:18px;color:#aab5cc}.message.error{color:#ff9b9b}.auth{display:grid;gap:12px}.auth input{width:100%;min-height:52px;padding:0 14px;border:1px solid #35415d;border-radius:13px;color:#fff;background:#090d17}.hidden{display:none}.busy main{border-color:#6c82b8}.busy .active::after{content:' ';display:inline-block;width:14px;height:14px;margin-left:8px;border:2px solid #91a5d4;border-top-color:transparent;border-radius:50%;animation:spin .8s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
@media(max-width:380px){.lines{grid-template-columns:1fr}main{border-radius:18px}}@media(prefers-reduced-motion:reduce){*,*::after{animation:none!important;transition:none!important}}
</style>
</head>
<body><main>
<h1>Line Switch</h1><p class="sub">Independent Better Agent control</p>
<section id="auth" class="auth hidden"><label for="token">Access key</label><input id="token" type="password" autocomplete="current-password"><button id="connect">Connect</button></section>
<section id="control" class="hidden"><div class="status"><span class="label">Active line</span><span id="active" class="active">—</span></div><div id="lines" class="lines"></div><div id="message" class="message" role="status" aria-live="polite"></div></section>
</main><script nonce="__NONCE__">
const auth=document.querySelector('#auth'),control=document.querySelector('#control'),tokenInput=document.querySelector('#token'),message=document.querySelector('#message');
let token=sessionStorage.getItem('line-switch-token')||location.hash.slice(1);if(location.hash)history.replaceState(null,'',location.pathname);
const headers=()=>({'Authorization':`Bearer ${token}`,'Content-Type':'application/json'});
async function api(path,options={}){const response=await fetch(path,{...options,headers:{...headers(),...(options.headers||{})},cache:'no-store'});if(response.status===401){sessionStorage.removeItem('line-switch-token');showAuth();throw new Error('Invalid access key')}const data=await response.json();if(!response.ok)throw new Error(data.error||'Request failed');return data}
function showAuth(){auth.classList.remove('hidden');control.classList.add('hidden');tokenInput.focus()}
function setBusy(value){document.body.classList.toggle('busy',value);document.querySelectorAll('#lines button').forEach(button=>button.disabled=value||button.classList.contains('current'))}
function render(state){auth.classList.add('hidden');control.classList.remove('hidden');document.querySelector('#active').textContent=state.active_line||'Unknown';const busy=['preparing','pending','accepted'].includes(state.request?.status);const lines=document.querySelector('#lines');lines.replaceChildren(...Object.keys(state.lines).map(line=>{const button=document.createElement('button');button.textContent=line;button.className=line===state.active_line?'current':'';button.disabled=busy||line===state.active_line||Boolean(state.incompatible[line]);button.onclick=()=>switchLine(line);return button}));setBusy(busy);message.className='message';message.textContent=busy?`Switching to ${state.request.target}…`:state.request?.status==='failed'?(state.request.error||'Switch failed'):state.pointer?.status==='reverted'?'Last switch was reverted':'';if(state.request?.status==='failed')message.classList.add('error')}
async function refresh(){try{render(await api('/api/state'))}catch(error){if(!control.classList.contains('hidden')){message.textContent=error.message;message.classList.add('error')}}}
async function switchLine(target){if(!confirm(`Switch to ${target}?`))return;setBusy(true);message.textContent=`Preparing ${target}…`;try{await api('/api/switch',{method:'POST',body:JSON.stringify({target})});await refresh()}catch(error){message.textContent=error.message;message.classList.add('error');setBusy(false)}}
document.querySelector('#connect').onclick=()=>{token=tokenInput.value.trim();sessionStorage.setItem('line-switch-token',token);refresh()};tokenInput.onkeydown=event=>{if(event.key==='Enter')document.querySelector('#connect').click()};
if(token){sessionStorage.setItem('line-switch-token',token);refresh()}else showAuth();setInterval(()=>{if(!control.classList.contains('hidden'))refresh()},1500);
</script></body></html>'''
