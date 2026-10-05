"""Read-only GitHub PR analyzer. Only sends GET requests to GitHub."""
import json
import os
import re
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, Response, render_template_string, request, stream_with_context

load_dotenv(Path(__file__).parent / ".env", override=True)

MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
MAX_DIFF = 60000
PR_RE = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")

app = Flask(__name__)


class StepError(Exception):
    """An error with a message that is safe to show on the page."""


# ---------- GitHub (GET only) ----------
def gh_get(url, diff=False):
    headers = {
        "Accept": "application/vnd.github.v3.diff" if diff else "application/vnd.github+json",
        "User-Agent": "pr-analyzer",
    }
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException as e:
        raise StepError(f"Could not reach GitHub: {e}")
    if r.status_code == 404:
        raise StepError("PR not found. Check the link. For a private repo, add a read-only GITHUB_TOKEN to .env.")
    if r.status_code == 401:
        raise StepError("GitHub rejected the token (401). Check GITHUB_TOKEN in .env.")
    if r.status_code == 403:
        raise StepError("GitHub blocked the request (403): rate limit or no read access. Add or fix GITHUB_TOKEN.")
    if not r.ok:
        raise StepError(f"GitHub error {r.status_code}: {r.text[:200]}")
    return r


def fetch_pr(owner, repo, number):
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}"
    meta = gh_get(url).json()
    diff = gh_get(url, diff=True).text
    truncated = len(diff) > MAX_DIFF
    return {
        "title": meta.get("title", ""),
        "body": (meta.get("body") or "")[:2000],
        "author": (meta.get("user") or {}).get("login", "unknown"),
        "files": meta.get("changed_files", 0),
        "additions": meta.get("additions", 0),
        "deletions": meta.get("deletions", 0),
        "url": meta.get("html_url", ""),
        "truncated": truncated,
    }, diff[:MAX_DIFF]


# ---------- AI ----------
def ask_ai(prompt):
    key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not key or key == "your-gemini-api-key":
        raise StepError("GOOGLE_API_KEY is missing from your .env file.")
    from langchain_google_genai import ChatGoogleGenerativeAI

    llm = ChatGoogleGenerativeAI(model=MODEL_NAME, temperature=0, google_api_key=key)
    for attempt in range(3):
        try:
            reply = llm.invoke(prompt).content
            break
        except Exception as e:
            busy = any(w in str(e) for w in ("503", "UNAVAILABLE"))
            if busy and attempt < 2:
                time.sleep(3 * (attempt + 1))  # wait 3s, then 6s, then try again
                continue
            raise StepError(f"AI error: {e}")
    if isinstance(reply, list):
        reply = "".join(p if isinstance(p, str) else p.get("text", "") for p in reply)
    text = re.sub(r"```(?:json)?", "", reply).strip()
    try:
        return json.loads(text)
    except ValueError:
        raise StepError("The AI reply was not valid JSON. Click Analyze PR to try again.")


ISSUE_SHAPE = (
    '{"issues":[{"severity":"high | medium | low","file":"path/to/file",'
    '"title":"short title","detail":"what is wrong and why it matters",'
    '"suggestion":"how to fix it"}]}'
)
COMMON = (
    "Only report real, specific issues that are visible in the diff. Never invent problems. "
    "Reply with JSON only, no markdown fences, in exactly this shape: " + ISSUE_SHAPE +
    " If nothing is found, return {\"issues\": []}."
)


def review_prompt(role, focus, ignore, title, diff):
    return (
        f"You are a {role}. Your single focus: {focus}. Ignore {ignore}.\n{COMMON}\n\n"
        f"PR title: {title}\n\nDiff:\n{diff}"
    )


def clean_issues(data):
    items = data.get("issues", []) if isinstance(data, dict) else []
    out = []
    for i in items or []:
        if not isinstance(i, dict):
            continue
        sev = str(i.get("severity", "low")).lower()
        out.append({
            "severity": sev if sev in ("high", "medium", "low") else "low",
            "file": str(i.get("file", "")),
            "title": str(i.get("title", "Untitled issue")),
            "detail": str(i.get("detail", "")),
            "suggestion": str(i.get("suggestion", "")),
        })
    return out


# ---------- Routes ----------
@app.get("/")
def index():
    return render_template_string(PAGE)


@app.post("/analyze")
def analyze():
    pr_url = (request.get_json(silent=True) or {}).get("pr_url", "")

    def event(step, status, **extra):
        return json.dumps({"step": step, "status": status, **extra}) + "\n"

    def run():
        step = "fetch"
        try:
            m = PR_RE.search(pr_url)
            if not m:
                raise StepError("Paste a full PR link, like https://github.com/owner/repo/pull/1")
            yield event("fetch", "running")
            pr, diff = fetch_pr(*m.groups())
            yield event("fetch", "done", pr={k: v for k, v in pr.items() if k != "body"})

            step = "bugs"
            yield event("bugs", "running")
            bugs = clean_issues(ask_ai(review_prompt(
                "senior code reviewer",
                "bugs: logic errors, crashes, wrong conditions, unhandled edge cases, broken error handling, race conditions",
                "style and security", pr["title"], diff)))
            yield event("bugs", "done", issues=bugs)

            step = "security"
            yield event("security", "running")
            sec = clean_issues(ask_ai(review_prompt(
                "security reviewer",
                "security problems: hardcoded secrets, injection (SQL, command, XSS), unsafe deserialization, "
                "missing auth checks, path traversal, weak crypto, sensitive data in logs",
                "bugs and style", pr["title"], diff)))
            yield event("security", "done", issues=sec)

            step = "summary"
            yield event("summary", "running")
            result = ask_ai(
                "You are a senior code reviewer writing the final verdict for a pull request.\n"
                f"PR title: {pr['title']}\nPR description: {pr['body']}\n"
                f"Bug findings: {json.dumps(bugs)}\nSecurity findings: {json.dumps(sec)}\n"
                "Use request_changes if any high severity finding exists. Reply with JSON only, "
                "no markdown fences, in exactly this shape: "
                '{"verdict":"approve | request_changes | comment",'
                '"summary":"what the PR does, how risky it is, what to fix first"} '
                "Write the summary in 3-4 sentences."
            )
            verdict = str(result.get("verdict", "comment")) if isinstance(result, dict) else "comment"
            if verdict not in ("approve", "request_changes", "comment"):
                verdict = "comment"
            if any(i["severity"] == "high" for i in bugs + sec):
                verdict = "request_changes"  # enforce the rule in code too
            summary = str(result.get("summary", "")) if isinstance(result, dict) else ""
            yield event("summary", "done", verdict=verdict, summary=summary)
        except StepError as e:
            yield event(step, "error", error=str(e))
        except Exception as e:
            yield event(step, "error", error=f"Unexpected error: {e}")

    return Response(
        stream_with_context(run()),
        mimetype="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


# ---------- Page ----------
PAGE = r"""{% raw %}<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Code Sentinel</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cpath d='M16 2 4 6.5v8.2C4 22.3 9 27.8 16 30c7-2.2 12-7.7 12-15.3V6.5z' fill='%232457d6'/%3E%3C/svg%3E">
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#1b1f24;--muted:#5b6570;--line:#d8dde3;--accent:#2457d6;
--ok:#1a7f45;--okbg:#e4f5ea;--bad:#c42b2b;--badbg:#fbe9e9;--warn:#9a6700;--warnbg:#fff3d1;--act:#e8efff}
@media (prefers-color-scheme:dark){:root{--bg:#14171b;--card:#1c2026;--text:#e8ebef;--muted:#99a3ae;--line:#323a44;
--accent:#7ca2ff;--ok:#5fd18b;--okbg:#17301f;--bad:#ff8585;--badbg:#3a1c1c;--warn:#f0c14b;--warnbg:#3a3015;--act:#1e2a45}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:760px;margin:0 auto;padding:32px 16px 64px}
.brand{display:flex;align-items:center;gap:10px;margin-bottom:28px}
.logo{color:var(--accent);flex:none}
.brand-name{font-size:1.3rem;font-weight:700;letter-spacing:-.01em}
h1{font-size:1.9rem;margin:0 0 6px;line-height:1.2}
.lead{color:var(--muted);margin:0 0 24px}
form{display:flex;gap:8px;flex-wrap:wrap}
input{flex:1 1 320px;padding:11px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--text);font:inherit}
button{padding:11px 18px;border:0;border-radius:8px;background:var(--accent);color:#fff;font:inherit;font-weight:600;cursor:pointer}
@media (prefers-color-scheme:dark){button{color:#0b1220}}
button:disabled{opacity:.55;cursor:not-allowed}
:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
#error{display:none;margin-top:14px;padding:12px 14px;border-radius:8px;background:var(--badbg);color:var(--bad);border:1px solid var(--bad)}
.steps{display:none;grid-template-columns:repeat(4,1fr);gap:8px;margin:22px 0}
@media (max-width:560px){.steps{grid-template-columns:repeat(2,1fr)}}
.step{padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);transition:background .2s}
.step b{display:block;font-size:.95rem}.step span{font-size:.85rem;color:var(--muted)}
.step.running{background:var(--act);border-color:var(--accent)}
.step.done{background:var(--okbg);border-color:var(--ok)}
.step.error{background:var(--badbg);border-color:var(--bad)}
.panel{margin:0 0 16px;padding:16px;border:1px solid var(--line);border-radius:10px;background:var(--card)}
.panel h2{font-size:1.1rem;margin:0 0 10px}
.verdict{border-width:2px}.verdict h2{font-size:1.35rem}
.verdict.approve{background:var(--okbg);border-color:var(--ok)}
.verdict.request_changes{background:var(--badbg);border-color:var(--bad)}
.verdict.comment{background:var(--warnbg);border-color:var(--warn)}
.meta{color:var(--muted);font-size:.92rem}
.issue{padding:12px 0;border-top:1px solid var(--line)}
.issue:first-of-type{border-top:0}
.sev{display:inline-block;padding:1px 8px;margin-right:8px;border-radius:99px;font-size:.8rem;font-weight:600;border:1px solid currentColor}
.high .sev{color:var(--bad)}.medium .sev{color:var(--warn)}.low .sev{color:var(--muted)}
code{display:block;font:.85rem ui-monospace,Menlo,Consolas,monospace;color:var(--muted);margin:4px 0;overflow-wrap:anywhere}
.issue p{margin:4px 0}.fix{color:var(--ok)}
a{color:var(--accent)}
.step{position:relative;overflow:hidden}
.step.running::after{content:"";position:absolute;left:0;bottom:0;height:3px;width:40%;background:var(--accent);animation:slide 1.2s ease-in-out infinite}
.step.running span::before{content:"";display:inline-block;width:10px;height:10px;margin-right:6px;border:2px solid var(--accent);border-top-color:transparent;border-radius:50%;animation:spin .8s linear infinite;vertical-align:-1px}
@keyframes slide{0%{transform:translateX(-100%)}100%{transform:translateX(250%)}}
@keyframes spin{to{transform:rotate(360deg)}}
.risk-low{--c:var(--ok)}.risk-medium{--c:var(--warn)}.risk-high{--c:var(--bad)}
.score-top{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.score-num{font-size:3rem;font-weight:700;line-height:1;color:var(--c)}
.score-label{font-weight:600;color:var(--c)}
.bar{height:10px;border-radius:99px;background:var(--line);margin:12px 0;overflow:hidden}
.bar i{display:block;height:100%;width:0;background:var(--c);transition:width 1s ease-out}
table{width:100%;border-collapse:collapse;font-size:.92rem}
th,td{text-align:left;padding:6px 4px;border-top:1px solid var(--line)}
.actions{margin:0 0 16px;display:flex;gap:8px}.actions:empty{display:none}
.ghost{background:transparent;color:var(--accent);border:1px solid var(--accent)}
details{margin-top:16px}summary{cursor:pointer;color:var(--muted)}
.hist{display:flex;justify-content:space-between;gap:8px;width:100%;text-align:left;background:transparent;color:var(--text);border:1px solid var(--line);margin-top:6px;font-weight:400}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{transition:none!important;animation:none!important}}
</style>
</head>
<body>
<main>
<header class="brand">
  <svg class="logo" viewBox="0 0 32 32" width="40" height="40" aria-hidden="true">
    <path d="M16 2 4 6.5v8.2C4 22.3 9 27.8 16 30c7-2.2 12-7.7 12-15.3V6.5z" fill="currentColor"/>
    <path d="m13 12-4 4 4 4M19 12l4 4-4 4" fill="none" stroke="var(--bg)" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
  </svg>
  <span class="brand-name">Code Sentinel</span>
</header>
<h1>Check a pull request before you merge it</h1>
<p class="lead">This app only reads the pull request. It never changes anything on GitHub.</p>
<form id="form">
  <input id="url" type="url" required placeholder="https://github.com/owner/repo/pull/12" aria-label="Pull request link">
  <button id="go" type="submit">Analyze PR</button>
</form>
<div id="history"></div>
<div id="error" class="error" role="alert"></div>
<div id="steps" class="steps" aria-live="polite"></div>
<div id="results">
  <div id="verdict"></div><div id="score"></div><div id="actions" class="actions"></div><div id="pr"></div><div id="bugs"></div><div id="security"></div>
</div>
</main>
<script>
const STEPS=[["fetch","1. Fetch PR"],["bugs","2. Check bugs"],["security","3. Check security"],["summary","4. Summarize"]];
const LABEL={waiting:"Waiting",running:"Working",done:"Done",error:"Failed"};
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function setStep(id,state){
  const el=$("step-"+id);
  el.className="step "+state;
  el.querySelector("span").textContent=LABEL[state];
}
function resetUI(){
  $("error").style.display="none";
  ["verdict","score","actions","pr","bugs","security"].forEach(k=>$(k).innerHTML="");
  $("steps").innerHTML=STEPS.map(([id,name])=>`<div class="step waiting" id="step-${id}"><b>${esc(name)}</b><span>Waiting</span></div>`).join("");
  $("steps").style.display="grid";
}
function showError(msg){const e=$("error");e.textContent=msg;e.style.display="block";}

function renderPr(pr){
  const safe=pr.url.startsWith("https://github.com/");
  const title=safe?`<a href="${esc(pr.url)}" target="_blank" rel="noopener">${esc(pr.title)}</a>`:esc(pr.title);
  $("pr").innerHTML=`<section class="panel"><h2>${title}</h2>
    <p class="meta">By ${esc(pr.author)} &middot; ${esc(pr.files)} files changed &middot; +${esc(pr.additions)} / -${esc(pr.deletions)}</p>
    ${pr.truncated?'<p class="meta">The diff was longer than 60,000 characters, so only the first part was analyzed.</p>':""}</section>`;
}
function issueHtml(i){
  return `<article class="issue ${esc(i.severity)}"><div><span class="sev">${esc(i.severity)}</span><strong>${esc(i.title)}</strong></div>
    <code>${esc(i.file)}</code><p>${esc(i.detail)}</p><p class="fix"><b>Fix:</b> ${esc(i.suggestion)}</p></article>`;
}
function renderIssues(id,issues){
  const name=id==="bugs"?"Bugs":"Security";
  const count=issues.length?`${issues.length} finding${issues.length>1?"s":""}`:"nothing found";
  $(id).innerHTML=`<section class="panel"><h2>${name}: ${count}</h2>${issues.map(issueHtml).join("")}</section>`;
}
const VERDICT={approve:"Looks good to merge",request_changes:"Changes needed",comment:"Worth a look"};
function renderVerdict(ev){
  $("verdict").innerHTML=`<section class="panel verdict ${esc(ev.verdict)}"><h2>${VERDICT[ev.verdict]}</h2><p>${esc(ev.summary)}</p></section>`;
}

let cur={};
const POINTS={high:25,medium:10,low:3};
const LEVEL={low:"Low risk",medium:"Medium risk",high:"High risk"};
function calcScore(all){
  const rows=["high","medium","low"].map(s=>{const n=all.filter(i=>i.severity===s).length;return {sev:s,n:n,pts:POINTS[s],total:n*POINTS[s]};});
  const score=Math.min(100,rows.reduce((a,r)=>a+r.total,0));
  return {rows:rows,score:score,level:score<=30?"low":score<=60?"medium":"high"};
}
function countUp(el,to){
  if(matchMedia("(prefers-reduced-motion:reduce)").matches||to===0){el.textContent=to;return;}
  const t0=performance.now();
  (function tick(t){const p=Math.min(1,(t-t0)/1000);el.textContent=Math.round(to*p);if(p<1)requestAnimationFrame(tick);})(t0);
}
function allIssues(d){return [].concat(d.bugs||[],d.security||[]);}
function renderScore(d){
  const r=calcScore(allIssues(d));
  $("score").innerHTML=`<section class="panel risk-${r.level}"><h2>Release risk</h2>
    <div class="score-top"><span class="score-num" id="num">0</span><span class="meta">/ 100</span><span class="score-label">${LEVEL[r.level]}</span></div>
    <div class="bar"><i id="fill"></i></div>
    <table><thead><tr><th>Severity</th><th>Findings</th><th>Points each</th><th>Total</th></tr></thead><tbody>
    ${r.rows.map(x=>`<tr><td>${x.sev}</td><td>${x.n}</td><td>${x.pts}</td><td>${x.total}</td></tr>`).join("")}</tbody></table>
    <p class="meta">Estimated risk from the issues found, capped at 100. It is not a guarantee.</p></section>`;
  requestAnimationFrame(()=>{$("fill").style.width=r.score+"%";});
  countUp($("num"),r.score);
}
function report(d){
  const r=calcScore(allIssues(d));
  const sec=(t,a)=>"## "+t+": "+(a.length?a.length+" finding"+(a.length>1?"s":""):"nothing found")+"\n\n"+
    a.map(i=>"### ["+i.severity+"] "+i.title+"\n`"+i.file+"`\n\n"+i.detail+"\n\n**Fix:** "+i.suggestion+"\n").join("\n");
  return "# Code Sentinel report\n\n**PR:** "+d.pr.title+" ("+d.pr.url+")\n**Author:** "+d.pr.author+" | "+d.pr.files+" files | +"+d.pr.additions+" / -"+d.pr.deletions+
    "\n\n**Verdict:** "+VERDICT[d.verdict]+"\n**Release risk:** "+r.score+"/100 ("+LEVEL[r.level]+")\n\n"+d.summary+"\n\n"+
    sec("Bugs",d.bugs||[])+"\n"+sec("Security",d.security||[])+"\n";
}
function download(d){
  const num=(d.pr.url.match(/pull\/(\d+)/)||[])[1]||"report";
  const a=document.createElement("a");
  a.href=URL.createObjectURL(new Blob([report(d)],{type:"text/markdown"}));
  a.download="code-sentinel-pr-"+num+".md";
  a.click();
  setTimeout(()=>URL.revokeObjectURL(a.href),1000);
}
function showActions(d){
  $("actions").innerHTML='<button type="button" class="ghost" id="dl">Download report</button>';
  $("dl").onclick=()=>download(d);
}
const HKEY="code-sentinel-history";
function loadHistory(){try{return JSON.parse(localStorage.getItem(HKEY))||[];}catch(e){return [];}}
function saveHistory(d){
  const list=loadHistory().filter(h=>h.pr.url!==d.pr.url);
  list.unshift(Object.assign({},d,{score:calcScore(allIssues(d)).score,when:new Date().toLocaleString()}));
  try{localStorage.setItem(HKEY,JSON.stringify(list.slice(0,10)));}catch(e){}
  renderHistory();
}
function renderHistory(){
  const list=loadHistory();
  $("history").innerHTML=list.length?`<details><summary>Recent reviews (${list.length})</summary>
    ${list.map((h,i)=>`<button type="button" class="hist" data-i="${i}"><span>${esc(h.pr.title)}</span><span class="meta">${esc(h.score)}/100 &middot; ${esc(h.when)}</span></button>`).join("")}
    <button type="button" class="ghost" id="clear" style="margin-top:8px">Clear history</button></details>`:"";
  document.querySelectorAll(".hist").forEach(b=>{b.onclick=()=>openSaved(list[b.dataset.i]);});
  const c=$("clear");if(c)c.onclick=()=>{try{localStorage.removeItem(HKEY);}catch(e){}renderHistory();};
}
function openSaved(d){
  resetUI();$("steps").style.display="none";cur=d;
  renderPr(d.pr);renderIssues("bugs",d.bugs||[]);renderIssues("security",d.security||[]);
  renderVerdict(d);renderScore(d);showActions(d);
  window.scrollTo({top:0,behavior:"smooth"});
}

function handle(ev){
  setStep(ev.step,ev.status);
  if(ev.status==="error"){showError(ev.error);return;}
  if(ev.status!=="done")return;
  if(ev.step==="fetch"){cur={pr:ev.pr};renderPr(ev.pr);}
  else if(ev.step==="summary"){
    cur.verdict=ev.verdict;cur.summary=ev.summary;
    renderVerdict(ev);renderScore(cur);showActions(cur);saveHistory(cur);
  }
  else{cur[ev.step]=ev.issues;renderIssues(ev.step,ev.issues);}
}

$("form").addEventListener("submit",async e=>{
  e.preventDefault();
  const btn=$("go");btn.disabled=true;btn.textContent="Analyzing...";resetUI();
  try{
    const res=await fetch("/analyze",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({pr_url:$("url").value.trim()})});
    const reader=res.body.getReader(),dec=new TextDecoder();
    let buf="";
    while(true){
      const {value,done}=await reader.read();
      if(done)break;
      buf+=dec.decode(value,{stream:true});
      const lines=buf.split("\n");buf=lines.pop();
      lines.filter(l=>l.trim()).forEach(l=>handle(JSON.parse(l)));
    }
  }catch(err){showError("Could not reach the app server: "+err.message);}
  btn.disabled=false;btn.textContent="Analyze PR";
});
renderHistory();
</script>
</body>
</html>{% endraw %}"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, threaded=True, debug=False)