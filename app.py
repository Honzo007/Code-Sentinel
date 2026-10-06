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
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cpath d='M16 2 4 6.5v8.2C4 22.3 9 27.8 16 30c7-2.2 12-7.7 12-15.3V6.5z' fill='%232f6fed'/%3E%3C/svg%3E">
<style>
:root{--bg:#f3f7fb;--card:#fff;--text:#1f2a37;--muted:#5d6b7e;--line:#dde5ee;--accent:#2f6fed;--accent-soft:#e8f0fe;
--ok:#1a7f55;--ok-soft:#e6f6ee;--bad:#c13a3a;--bad-soft:#fdecec;--warn:#9a5f08;--warn-soft:#fff4d6;color-scheme:light}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.top{background:#fff;border-bottom:1px solid var(--line)}
.wrap{max-width:960px;margin:0 auto;padding:0 20px}
.brand{display:flex;align-items:center;gap:10px;height:64px}
.logo{color:var(--accent);flex:none}
.brand-name{font-weight:700;font-size:1.15rem}
main{padding:28px 20px 72px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:0 1px 2px rgba(31,42,55,.05)}
.hero{background:#eef4ff;border-color:#cfdffb;padding:30px 24px}
h1{font-size:1.8rem;line-height:1.25;margin:0 0 6px}
h2{font-size:1.1rem;margin:0 0 10px}
.lead{color:var(--muted);margin:0 0 20px}
form{display:flex;gap:10px;flex-wrap:wrap}
input{flex:1 1 320px;padding:13px 14px;border:1px solid #bccde3;border-radius:10px;background:#fff;color:var(--text);font:inherit}
button{padding:13px 22px;border:0;border-radius:10px;background:var(--accent);color:#fff;font:inherit;font-weight:600;cursor:pointer}
#go:hover:not(:disabled){background:#2559c7}
button:disabled{opacity:.6;cursor:not-allowed}
:focus-visible{outline:3px solid #8fb3ff;outline-offset:2px}
.hint{margin:10px 0 0;font-size:.9rem;color:var(--muted)}
#error{display:none;margin-top:14px;padding:12px 14px;border-radius:10px;background:var(--bad-soft);color:var(--bad);border:1px solid #f1b9b9;overflow-wrap:anywhere}
.ghost{background:transparent;color:var(--accent);border:1px solid var(--accent);padding:10px 16px}
.ghost:hover{background:var(--accent-soft)}
details{margin:0 0 16px}summary{cursor:pointer;color:var(--muted)}
.hist{display:flex;justify-content:space-between;gap:8px;width:100%;text-align:left;background:#fff;color:var(--text);border:1px solid var(--line);margin-top:6px;font-weight:400;padding:10px 14px}
.hist:hover{background:var(--accent-soft)}
.steps{display:none;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:16px}
@media (max-width:640px){.steps{grid-template-columns:repeat(2,1fr)}}
.step{position:relative;overflow:hidden;display:flex;gap:10px;align-items:center;padding:12px;background:#fff;border:1px solid var(--line);border-radius:12px}
.num{flex:none;width:28px;height:28px;border-radius:50%;background:var(--line);color:var(--muted);display:grid;place-items:center;font-style:normal;font-weight:600;font-size:.85rem}
.step b{display:block;font-size:.95rem}.step span{font-size:.85rem;color:var(--muted)}
.step.running{background:var(--accent-soft);border-color:#b9cffb}.step.running .num{background:var(--accent);color:#fff}
.step.running::after{content:"";position:absolute;left:0;bottom:0;height:3px;width:40%;background:var(--accent);animation:slide 1.2s ease-in-out infinite}
.step.running span::before{content:"";display:inline-block;width:10px;height:10px;margin-right:6px;border:2px solid var(--accent);border-top-color:transparent;border-radius:50%;animation:spin .8s linear infinite;vertical-align:-1px}
.step.done{background:var(--ok-soft);border-color:#b5e3cc}.step.done .num{background:var(--ok);color:#fff}
.step.error{background:var(--bad-soft);border-color:#f1b9b9}.step.error .num{background:var(--bad);color:#fff}
@keyframes slide{0%{transform:translateX(-100%)}100%{transform:translateX(250%)}}
@keyframes spin{to{transform:rotate(360deg)}}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}.grid2>div:empty{display:none}
@media (max-width:700px){.grid2{grid-template-columns:1fr}}
.label{font-size:.85rem;color:var(--muted);margin-bottom:2px}
.big{font-size:1.5rem;font-weight:700;margin-bottom:6px}
.v-approve{border-left:6px solid var(--ok)}.v-approve .big{color:var(--ok)}
.v-request_changes{border-left:6px solid var(--bad)}.v-request_changes .big{color:var(--bad)}
.v-comment{border-left:6px solid var(--warn)}.v-comment .big{color:var(--warn)}
.risk-low{--c:var(--ok)}.risk-medium{--c:var(--warn)}.risk-high{--c:var(--bad)}
.score-top{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.score-num{font-size:2.6rem;font-weight:700;line-height:1;color:var(--c)}
.score-label{font-weight:600;color:var(--c);margin-left:auto}
.bar{height:10px;border-radius:99px;background:#e6ecf3;margin:12px 0;overflow:hidden}
.bar i{display:block;height:100%;width:0;background:var(--c);transition:width 1s ease-out}
table{width:100%;border-collapse:collapse;font-size:.9rem;margin-top:8px}
th,td{text-align:left;padding:6px 4px;border-top:1px solid var(--line)}
.actions{margin:0 0 16px}.actions:empty{display:none}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.chip{background:var(--accent-soft);color:#1d4fb8;padding:3px 10px;border-radius:99px;font-size:.85rem}
.chip.add{background:var(--ok-soft);color:var(--ok)}.chip.del{background:var(--bad-soft);color:var(--bad)}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}
.tab{background:#fff;color:var(--text);border:1px solid var(--line);padding:8px 14px;font-weight:500}
.tab:hover:not(.active){background:var(--accent-soft)}
.tab.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.tab .n{margin-left:6px;opacity:.8}
.finding{border:1px solid var(--line);border-left-width:5px;border-radius:10px;padding:14px 16px;margin-bottom:12px;background:#fff}
.sev-high{border-left-color:var(--bad)}.sev-medium{border-left-color:var(--warn)}.sev-low{border-left-color:#7b8da3}
.f-head{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.pill{padding:2px 10px;border-radius:99px;font-size:.8rem;font-weight:600}
.sev-high .pill{background:var(--bad-soft);color:var(--bad)}.sev-medium .pill{background:var(--warn-soft);color:var(--warn)}.sev-low .pill{background:#eef1f5;color:#52627a}
.cat{font-size:.8rem;color:var(--muted);border:1px solid var(--line);border-radius:6px;padding:1px 8px}
.f-file{display:inline-block;margin:8px 0;padding:3px 8px;border-radius:6px;background:var(--bg);color:var(--muted);font:.85rem ui-monospace,Menlo,Consolas,monospace;overflow-wrap:anywhere}
dl{margin:4px 0 0;display:grid;grid-template-columns:110px 1fr;gap:8px 12px}
dt{font-weight:600;color:var(--muted);font-size:.9rem}dd{margin:0}
dd.fix{background:var(--ok-soft);padding:6px 10px;border-radius:8px}
@media (max-width:560px){dl{grid-template-columns:1fr;gap:2px}dt{margin-top:8px}}
a{color:#1d4fb8}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{transition:none!important;animation:none!important}}
</style>
</head>
<body>
<header class="top"><div class="wrap brand">
  <svg class="logo" viewBox="0 0 32 32" width="36" height="36" aria-hidden="true">
    <path d="M16 2 4 6.5v8.2C4 22.3 9 27.8 16 30c7-2.2 12-7.7 12-15.3V6.5z" fill="currentColor"/>
    <path d="m13 12-4 4 4 4M19 12l4 4-4 4" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
  </svg>
  <span class="brand-name">Code Sentinel</span>
</div></header>
<main class="wrap">
<section class="card hero">
  <h1>Check a pull request before you merge it</h1>
  <p class="lead">Paste a GitHub pull request link. This app only reads the pull request and never changes anything on GitHub.</p>
  <form id="form">
    <input id="url" type="url" required placeholder="https://github.com/owner/repo/pull/12" aria-label="Pull request link">
    <button id="go" type="submit">Analyze PR</button>
  </form>
  <div id="error" role="alert"></div>
</section>
<div id="history"></div>
<div id="steps" class="steps" aria-live="polite"></div>
<div id="results" hidden>
  <div class="grid2"><div id="verdict"></div><div id="score"></div></div>
  <div id="actions" class="actions"></div>
  <div id="pr"></div>
  <div id="findings"></div>
</div>
</main>
<script>
const STEPS=[["fetch","Fetch PR"],["bugs","Check bugs"],["security","Check security"],["summary","Summarize"]];
const LABEL={waiting:"Waiting",running:"Working",done:"Done",error:"Failed"};
const SEV={high:"High",medium:"Medium",low:"Low"};
const ORDER={high:0,medium:1,low:2};
const POINTS={high:25,medium:10,low:3};
const LEVEL={low:"Low risk",medium:"Medium risk",high:"High risk"};
const VERDICT={approve:"Looks good to merge",request_changes:"Changes needed",comment:"Worth a look"};
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let cur={},tab="all";

function setStep(id,state){const el=$("step-"+id);el.className="step "+state;el.querySelector("span").textContent=LABEL[state];}
function resetUI(){
  $("error").style.display="none";
  $("results").hidden=true;
  ["verdict","score","actions","pr","findings"].forEach(k=>$(k).innerHTML="");
  $("steps").innerHTML=STEPS.map(([id,name],n)=>`<div class="step waiting" id="step-${id}"><i class="num">${n+1}</i><div><b>${esc(name)}</b><span>Waiting</span></div></div>`).join("");
  $("steps").style.display="grid";
}
function showError(msg){const e=$("error");e.textContent=msg;e.style.display="block";}

function renderPr(pr){
  const safe=String(pr.url).startsWith("https://github.com/");
  const t=safe?`<a href="${esc(pr.url)}" target="_blank" rel="noopener">${esc(pr.title)}</a>`:esc(pr.title);
  $("pr").innerHTML=`<section class="card"><div class="label">Pull request</div><h2>${t}</h2>
    <div class="chips"><span class="chip">By ${esc(pr.author)}</span><span class="chip">${esc(pr.files)} files changed</span><span class="chip add">+${esc(pr.additions)}</span><span class="chip del">-${esc(pr.deletions)}</span></div>
    ${pr.truncated?'<p class="hint">The diff was longer than 60,000 characters, so only the first part was analyzed.</p>':""}</section>`;
}
function renderVerdict(d){
  $("verdict").innerHTML=`<section class="card v-${esc(d.verdict)}"><div class="label">Verdict</div><div class="big">${esc(VERDICT[d.verdict])}</div><p>${esc(d.summary)}</p></section>`;
}
function calcScore(all){
  const rows=["high","medium","low"].map(s=>{const n=all.filter(i=>i.severity===s).length;return {sev:s,n:n,pts:POINTS[s],total:n*POINTS[s]};});
  const score=Math.min(100,rows.reduce((a,r)=>a+r.total,0));
  return {rows:rows,score:score,level:score<=30?"low":score<=60?"medium":"high"};
}
function allIssues(d){return [].concat(d.bugs||[],d.security||[]);}
function countUp(el,to){
  if(matchMedia("(prefers-reduced-motion:reduce)").matches||to===0){el.textContent=to;return;}
  const t0=performance.now();
  (function tick(t){const p=Math.min(1,(t-t0)/1000);el.textContent=Math.round(to*p);if(p<1)requestAnimationFrame(tick);})(t0);
}
function renderScore(d){
  const r=calcScore(allIssues(d));
  $("score").innerHTML=`<section class="card risk-${r.level}"><div class="label">Release risk</div>
    <div class="score-top"><span class="score-num" id="num">0</span><span class="label">/ 100</span><span class="score-label">${LEVEL[r.level]}</span></div>
    <div class="bar"><i id="fill"></i></div>
    <details><summary>How is this calculated?</summary>
    <table><thead><tr><th>Severity</th><th>Findings</th><th>Points each</th><th>Total</th></tr></thead><tbody>
    ${r.rows.map(x=>`<tr><td>${x.sev}</td><td>${x.n}</td><td>${x.pts}</td><td>${x.total}</td></tr>`).join("")}</tbody></table>
    <p class="hint">Estimated risk from the issues found, capped at 100. It is not a guarantee.</p></details></section>`;
  requestAnimationFrame(()=>{$("fill").style.width=r.score+"%";});
  countUp($("num"),r.score);
}
function findingHtml(i){
  return `<article class="finding sev-${esc(i.severity)}"><div class="f-head"><span class="pill">${esc(SEV[i.severity]||i.severity)}</span><span class="cat">${esc(i.cat)}</span><strong>${esc(i.title)}</strong></div>
    ${i.file?`<div class="f-file">${esc(i.file)}</div>`:""}
    <dl><dt>What is wrong</dt><dd>${esc(i.detail)}</dd><dt>How to fix</dt><dd class="fix">${esc(i.suggestion)}</dd></dl></article>`;
}
function renderFindings(){
  const b=(cur.bugs||[]).map(i=>Object.assign({cat:"Bug"},i));
  const s=(cur.security||[]).map(i=>Object.assign({cat:"Security"},i));
  const it={bugs:b,security:s,all:b.concat(s).sort((x,y)=>ORDER[x.severity]-ORDER[y.severity])};
  const wait={bugs:cur.bugs===undefined,security:cur.security===undefined};
  wait.all=wait.bugs||wait.security;
  const tabs=[["all","All"],["bugs","Bugs"],["security","Security"]];
  const list=it[tab];
  $("findings").innerHTML=`<section class="card"><h2>Findings</h2>
    <div class="tabs" role="tablist">${tabs.map(([k,n])=>`<button type="button" role="tab" aria-selected="${tab===k}" class="tab${tab===k?" active":""}" data-tab="${k}">${n}<span class="n">${wait[k]?"...":it[k].length}</span></button>`).join("")}</div>
    ${list.length?list.map(findingHtml).join(""):`<p class="hint">${wait[tab]?"Still checking...":"No findings in this category."}</p>`}</section>`;
  document.querySelectorAll(".tab").forEach(btn=>{btn.onclick=()=>{tab=btn.dataset.tab;renderFindings();};});
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
  $("history").innerHTML=list.length?`<details class="card"><summary>Recent reviews (${list.length})</summary>
    ${list.map((h,i)=>`<button type="button" class="hist" data-i="${i}"><span>${esc(h.pr.title)}</span><span class="label">${esc(h.score)}/100 &middot; ${esc(h.when)}</span></button>`).join("")}
    <button type="button" class="ghost" id="clear" style="margin-top:10px">Clear history</button></details>`:"";
  document.querySelectorAll(".hist").forEach(b=>{b.onclick=()=>openSaved(list[b.dataset.i]);});
  const c=$("clear");if(c)c.onclick=()=>{try{localStorage.removeItem(HKEY);}catch(e){}renderHistory();};
}
function openSaved(d){
  resetUI();$("steps").style.display="none";cur=d;tab="all";$("results").hidden=false;
  renderPr(d.pr);renderFindings();renderVerdict(d);renderScore(d);showActions(d);
  window.scrollTo({top:0,behavior:"smooth"});
}

function handle(ev){
  setStep(ev.step,ev.status);
  if(ev.status==="error"){showError(ev.error);return;}
  if(ev.status!=="done")return;
  if(ev.step==="fetch"){cur={pr:ev.pr};tab="all";$("results").hidden=false;renderPr(ev.pr);}
  else if(ev.step==="summary"){
    cur.verdict=ev.verdict;cur.summary=ev.summary;
    renderVerdict(cur);renderScore(cur);showActions(cur);saveHistory(cur);
  }
  else{cur[ev.step]=ev.issues;renderFindings();}
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