from __future__ import annotations

import hmac
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from brainkit.application.services import BrainkitService
from brainkit.domain.model import BrainkitError, ValidationError


def run_web(
    service: BrainkitService,
    *,
    host: str,
    port: int,
    consumer: str,
    token_env: str = "",
    instance_id: str = "",
) -> None:
    if not 1 <= port <= 65535:
        raise ValidationError("Web viewer port must be between 1 and 65535")
    if consumer not in {"human", "local", "cloud"}:
        raise ValidationError("Web viewer consumer is invalid")
    token = os.environ.get(token_env) if token_env else None
    if host not in {"127.0.0.1", "localhost", "::1"} and not token:
        raise ValidationError(
            "Remote web viewer binding requires --token-env with a populated variable"
        )
    server = BrainkitWebServer((host, port), BrainkitWebHandler)
    server.service = service
    server.consumer = consumer
    server.token = token
    server.instance_id = instance_id
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


class BrainkitWebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    service: BrainkitService
    consumer: str
    token: str | None
    instance_id: str


class BrainkitWebHandler(BaseHTTPRequestHandler):
    server: BrainkitWebServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._send_html(WEB_VIEWER_HTML)
            return
        if parsed.path == "/api/health":
            self._send_json(
                {
                    "ok": True,
                    "service": "brainkit-web",
                    "instance_id": self.server.instance_id,
                }
            )
            return
        if not self._authorized():
            self._send_json(
                {"ok": False, "error": {"code": "unauthorized"}},
                status=HTTPStatus.UNAUTHORIZED,
            )
            return
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/status":
                value = self.server.service.reader_status(
                    consumer=self.server.consumer
                )
            elif parsed.path == "/api/graph":
                value = self.server.service.graph_data(
                    consumer=self.server.consumer
                )
            elif parsed.path == "/api/search":
                phrase = _one(query, "q")
                limit = int(_one(query, "limit", "20"))
                value = self.server.service.search(
                    phrase,
                    limit=limit,
                    consumer=self.server.consumer,
                )
            elif parsed.path == "/api/proposals":
                value = self.server.service.proposals_for_consumer(
                    _one(query, "status", "") or None,
                    consumer=self.server.consumer,
                )
            elif parsed.path == "/api/resource":
                value = self.server.service.read_resource(
                    _one(query, "id"), consumer=self.server.consumer
                )
            elif parsed.path == "/api/sources":
                value = self.server.service.browse_sources(
                    consumer=self.server.consumer,
                    limit=int(_one(query, "limit", "500")),
                )
            elif parsed.path == "/api/pages":
                value = self.server.service.browse_pages(
                    consumer=self.server.consumer,
                    limit=int(_one(query, "limit", "500")),
                )
            elif parsed.path == "/api/timeline":
                value = self.server.service.timeline(
                    consumer=self.server.consumer,
                    limit=int(_one(query, "limit", "500")),
                )
            elif parsed.path == "/api/integrations":
                value = self.server.service.integration_status()
            else:
                self._send_json(
                    {"ok": False, "error": {"code": "not_found"}},
                    status=HTTPStatus.NOT_FOUND,
                )
                return
        except (ValueError, BrainkitError) as exc:
            code = getattr(exc, "code", "invalid_request")
            details = getattr(exc, "details", {})
            self._send_json(
                {
                    "ok": False,
                    "error": {
                        "code": code,
                        "message": str(exc),
                        "details": details,
                    },
                },
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        self._send_json({"ok": True, "result": value})

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _authorized(self) -> bool:
        if not self.server.token:
            return True
        return hmac.compare_digest(
            self.headers.get("Authorization", ""), f"Bearer {self.server.token}"
        )

    def _send_json(
        self, value: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK
    ) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self._security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, value: str) -> None:
        body = value.encode("utf-8")
        self.send_response(HTTPStatus.OK.value)
        self._security_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'",
        )


def _one(query: dict[str, list[str]], key: str, default: str | None = None) -> str:
    values = query.get(key)
    if values:
        return values[0]
    if default is not None:
        return default
    raise ValidationError("Web API query parameter is missing", details={"field": key})


WEB_VIEWER_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>brainkit</title>
<style>
:root{color-scheme:dark;--bg:#090d14;--panel:#111824;--panel2:#151f2e;--line:#27364b;--text:#e8eef8;--muted:#8fa1b8;--blue:#63a5ff;--cyan:#55d6be;--amber:#f4bf6a;--red:#ff7b83}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 75% -15%,#18304d 0,transparent 38%),var(--bg);color:var(--text);font:14px Inter,ui-sans-serif,system-ui,sans-serif;height:100vh;overflow:hidden}
button,input{font:inherit}.shell{height:100vh;display:grid;grid-template-rows:64px 1fr}.top{display:flex;align-items:center;gap:18px;padding:0 22px;border-bottom:1px solid var(--line);background:#090d14df;backdrop-filter:blur(18px)}
.brand{font-size:18px;font-weight:750;letter-spacing:-.03em}.brand span{color:var(--cyan)}.search{position:relative;flex:1;max-width:720px}.search input{width:100%;border:1px solid var(--line);background:#111824;color:var(--text);border-radius:10px;padding:11px 42px 11px 14px;outline:none}.search input:focus{border-color:var(--blue);box-shadow:0 0 0 3px #63a5ff1c}.key{position:absolute;right:10px;top:9px;color:var(--muted);border:1px solid var(--line);border-radius:5px;padding:2px 6px;font-size:11px}
.nav{display:flex;gap:4px}.view-btn{border:1px solid transparent;background:transparent;color:var(--muted);padding:7px 9px;border-radius:7px;cursor:pointer}.view-btn:hover,.view-btn.active{color:var(--text);background:var(--panel2);border-color:var(--line)}
.health{margin-left:auto;display:flex;align-items:center;gap:8px;color:var(--muted)}.dot{width:8px;height:8px;border-radius:50%;background:var(--cyan);box-shadow:0 0 12px var(--cyan)}
.layout{min-height:0;display:grid;grid-template-columns:270px minmax(360px,1fr) 350px}.side,.detail{overflow:auto;background:#0d131d}.side{border-right:1px solid var(--line);padding:18px}.detail{border-left:1px solid var(--line);padding:18px}.main{min-width:0;min-height:0;position:relative;background:linear-gradient(#ffffff05 1px,transparent 1px),linear-gradient(90deg,#ffffff05 1px,transparent 1px);background-size:36px 36px}
h2{font-size:11px;text-transform:uppercase;letter-spacing:.14em;color:var(--muted);margin:20px 0 10px}.stats{display:grid;grid-template-columns:1fr 1fr;gap:8px}.stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px}.stat b{font-size:22px;display:block;letter-spacing:-.04em}.stat span{font-size:11px;color:var(--muted)}
.rows{display:grid;gap:7px}.row{display:flex;justify-content:space-between;gap:12px;padding:8px 10px;border-radius:7px;background:var(--panel);color:var(--muted)}.row strong{color:var(--text)}.badge{border-radius:999px;padding:3px 8px;background:#243248;color:#aecaef;font-size:11px}.badge.good{background:#173b35;color:#70e0c8}.badge.warn{background:#49361c;color:#ffd183}.badge.bad{background:#492127;color:#ff9ba2}
canvas{width:100%;height:100%;display:block}.graph-meta{position:absolute;left:16px;top:16px;background:#0b111bd9;border:1px solid var(--line);border-radius:10px;padding:10px 12px;color:var(--muted);backdrop-filter:blur(12px)}.graph-meta b{color:var(--text)}
.graph-view{position:absolute;inset:0}.collection{position:absolute;inset:0;overflow:auto;padding:22px;background:#0b111b}.hidden{display:none}.collection-head{display:flex;align-items:end;justify-content:space-between;margin-bottom:16px}.collection-head h1{margin:0;font-size:24px}.collection-head span{color:var(--muted)}.collection-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px}.item-card{border:1px solid var(--line);background:var(--panel);color:var(--text);padding:13px;border-radius:10px;text-align:left;cursor:pointer}.item-card:hover{border-color:var(--blue);transform:translateY(-1px)}.item-card b{display:block;margin-bottom:7px}.item-card small{display:block;color:var(--muted);line-height:1.45}.item-card .badge{display:inline-block;margin-top:9px}
.result{display:block;width:100%;text-align:left;border:1px solid transparent;background:var(--panel);color:var(--text);border-radius:9px;padding:11px;margin-bottom:7px;cursor:pointer}.result:hover{border-color:var(--blue);background:var(--panel2)}.result small{display:block;color:var(--muted);margin-top:4px}.empty{color:var(--muted);padding:14px;border:1px dashed var(--line);border-radius:9px}
.title{font-size:22px;letter-spacing:-.035em;margin:4px 0}.meta{color:var(--muted);margin-bottom:14px}.content{white-space:pre-wrap;word-break:break-word;line-height:1.6;font:13px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;min-height:180px}.proposal{padding:10px;border:1px solid var(--line);border-radius:9px;margin-bottom:7px}.proposal b{display:block}.proposal small{color:var(--muted)}
.loading{opacity:.55}.footer-note{position:absolute;right:14px;bottom:12px;color:var(--muted);font-size:11px;background:#0b111bc9;padding:5px 8px;border-radius:6px}@media(max-width:950px){.layout{grid-template-columns:220px 1fr}.detail{position:absolute;right:0;top:64px;bottom:0;width:min(88vw,380px);transform:translateX(100%);transition:.2s;z-index:5}.detail.open{transform:none}}@media(max-width:680px){.layout{grid-template-columns:1fr}.side{display:none}.top{padding:0 12px}.health{display:none}}
</style>
</head>
<body>
<div class="shell">
  <header class="top"><div class="brand">brain<span>kit</span></div><nav class="nav"><button class="view-btn active" data-view="graph">Graph</button><button class="view-btn" data-view="sources">Sources</button><button class="view-btn" data-view="pages">Wiki</button><button class="view-btn" data-view="timeline">Timeline</button><button class="view-btn" data-view="integrations">Services</button></nav><div class="search"><input id="search" placeholder="Search the compiled brain…" autocomplete="off"><span class="key">⌘ K</span></div><div class="health"><span class="dot"></span><span id="health">loading</span></div></header>
  <div class="layout">
    <aside class="side"><h2>Vault</h2><div class="stats" id="stats"></div><h2>Freshness</h2><div class="rows" id="freshness"></div><h2>Branches</h2><div class="rows" id="branches"></div><h2>Review queue</h2><div id="proposals"></div></aside>
    <main class="main"><div class="graph-view" id="graphView"><canvas id="graph"></canvas><div class="graph-meta" id="graphMeta">Loading graph…</div><div class="footer-note">drag to pan · wheel to zoom · click a node</div></div><section class="collection hidden" id="collection"><div class="collection-head"><h1 id="collectionTitle">Sources</h1><span id="collectionMeta"></span></div><div class="collection-grid" id="collectionGrid"></div></section></main>
    <aside class="detail" id="detail"><h2>Inspector</h2><div id="results"><div class="empty">Select a graph node or search the vault.</div></div></aside>
  </div>
</div>
<script>
const state={graph:null,nodes:[],edges:[],scale:1,ox:0,oy:0,drag:false,last:null,selected:null,cache:{}};
async function api(path){let token=localStorage.getItem('brainkit-token')||'';let headers=token?{Authorization:'Bearer '+token}:{};let response=await fetch(path,{headers});if(response.status===401){token=prompt('Access token')||'';if(token){localStorage.setItem('brainkit-token',token);return api(path)}}let data=await response.json();if(!data.ok)throw new Error(data.error?.message||data.error?.code||'Request failed');return data.result}
function escText(el,value){el.textContent=value??'';return el}
async function load(){try{let [status,graph,proposals]=await Promise.all([api('/api/status'),api('/api/graph'),api('/api/proposals?status=pending')]);renderStatus(status);renderProposals(proposals);prepareGraph(graph);document.getElementById('health').textContent=status.healthy?'healthy':'needs attention'}catch(error){document.getElementById('health').textContent=error.message}}
function renderStatus(s){let stats=document.getElementById('stats');stats.replaceChildren(...[['Sources',s.sources],['Wiki pages',s.wiki_pages],['Pending',s.pending],['Lint errors',s.lint_errors]].map(([label,value])=>{let d=document.createElement('div');d.className='stat';d.innerHTML='<b></b><span></span>';escText(d.querySelector('b'),value);escText(d.querySelector('span'),label);return d}));let fresh=document.getElementById('freshness');fresh.replaceChildren(...Object.entries(s.freshness).map(([name,value])=>row(name,value,name==='fresh'?'good':name==='stale'?'warn':'')));let branches=document.getElementById('branches');branches.replaceChildren(...Object.entries(s.by_branch).map(([name,value])=>row(name,value,'')))}
function row(name,value,kind){let d=document.createElement('div');d.className='row';let n=document.createElement('strong');let v=document.createElement('span');v.className='badge '+kind;escText(n,name);escText(v,value);d.append(n,v);return d}
function renderProposals(data){let root=document.getElementById('proposals');if(!data.proposals.length){root.innerHTML='<div class="empty">Nothing waiting.</div>';return}root.replaceChildren(...data.proposals.slice(0,8).map(p=>{let d=document.createElement('div');d.className='proposal';d.tabIndex=0;let b=document.createElement('b');let s=document.createElement('small');escText(b,p.destination_branch);escText(s,p.reason||p.proposal_id);d.append(b,s);d.onclick=()=>showResource('raw:'+p.source_hash);return d}))}
function hash(value){let h=2166136261;for(let i=0;i<value.length;i++){h^=value.charCodeAt(i);h=Math.imul(h,16777619)}return h>>>0}
function prepareGraph(graph){state.graph=graph;let degree={};for(let e of graph.edges){degree[e.source]=(degree[e.source]||0)+1;degree[e.target]=(degree[e.target]||0)+1}let nodes=[...graph.nodes].sort((a,b)=>(degree[b.id]||0)-(degree[a.id]||0));let hidden=Math.max(0,nodes.length-900);nodes=nodes.slice(0,900);let ids=new Set(nodes.map(n=>n.id));let kinds=[...new Set(nodes.map(n=>n.kind))];state.nodes=nodes.map((n,i)=>{let band=kinds.indexOf(n.kind)+1;let angle=((hash(n.id)%100000)/100000)*Math.PI*2;let radius=70+band*85+(hash(n.id+'r')%90);return {...n,x:Math.cos(angle)*radius,y:Math.sin(angle)*radius,r:n.kind==='raw'?4:6}});state.edges=graph.edges.filter(e=>ids.has(e.source)&&ids.has(e.target));let meta=document.getElementById('graphMeta');escText(meta,`${graph.nodes.length} nodes · ${graph.edges.length} edges${hidden?` · ${hidden} hidden for rendering`:''}${graph.redacted_nodes?` · ${graph.redacted_nodes} private`:''}`);resize();draw()}
const canvas=document.getElementById('graph'),ctx=canvas.getContext('2d');function resize(){let r=canvas.getBoundingClientRect(),d=devicePixelRatio||1;canvas.width=r.width*d;canvas.height=r.height*d;ctx.setTransform(d,0,0,d,0,0);draw()}addEventListener('resize',resize);
function screen(n){let r=canvas.getBoundingClientRect();return{x:r.width/2+state.ox+n.x*state.scale,y:r.height/2+state.oy+n.y*state.scale}}
function draw(){if(!state.nodes.length)return;let r=canvas.getBoundingClientRect();ctx.clearRect(0,0,r.width,r.height);let map=new Map(state.nodes.map(n=>[n.id,n]));ctx.lineWidth=.7;ctx.strokeStyle='#31415888';for(let e of state.edges){let a=map.get(e.source),b=map.get(e.target);if(!a||!b)continue;let p=screen(a),q=screen(b);ctx.beginPath();ctx.moveTo(p.x,p.y);ctx.lineTo(q.x,q.y);ctx.stroke()}for(let n of state.nodes){let p=screen(n);let selected=state.selected===n.id;ctx.beginPath();ctx.arc(p.x,p.y,(n.r+(selected?3:0))*Math.max(.75,state.scale*.35),0,Math.PI*2);ctx.fillStyle=selected?'#fff':n.kind==='raw'?'#55d6be':n.kind==='concept'?'#63a5ff':n.kind==='entity'?'#f4bf6a':'#b49cff';ctx.fill();if(state.scale>1.35||selected){ctx.fillStyle='#dfe8f5';ctx.font='11px system-ui';ctx.fillText(n.label,p.x+9,p.y+4)}}}
function nodeAt(x,y){let best=null,dist=14;for(let n of state.nodes){let p=screen(n),d=Math.hypot(x-p.x,y-p.y);if(d<dist){dist=d;best=n}}return best}
canvas.addEventListener('pointerdown',e=>{state.drag=true;state.last={x:e.clientX,y:e.clientY};canvas.setPointerCapture(e.pointerId)});canvas.addEventListener('pointermove',e=>{if(!state.drag)return;state.ox+=e.clientX-state.last.x;state.oy+=e.clientY-state.last.y;state.last={x:e.clientX,y:e.clientY};draw()});canvas.addEventListener('pointerup',e=>{let r=canvas.getBoundingClientRect(),node=nodeAt(e.clientX-r.left,e.clientY-r.top);state.drag=false;if(node)selectNode(node)});canvas.addEventListener('wheel',e=>{e.preventDefault();state.scale=Math.max(.25,Math.min(4,state.scale*(e.deltaY<0?1.12:.89)));draw()},{passive:false});
async function selectNode(node){state.selected=node.id;draw();await showResource(node.id)}
async function showResource(id){let root=document.getElementById('results');root.classList.add('loading');try{let item=await api('/api/resource?id='+encodeURIComponent(id));root.replaceChildren();let title=document.createElement('div');title.className='title';let meta=document.createElement('div');meta.className='meta';let pre=document.createElement('pre');pre.className='content';escText(title,item.title);escText(meta,`${item.kind} · ${item.privacy} · ${item.path}`);escText(pre,item.content);root.append(title,meta,pre);document.getElementById('detail').classList.add('open')}catch(error){root.innerHTML='<div class="empty"></div>';escText(root.firstChild,error.message)}finally{root.classList.remove('loading')}}
let timer;document.getElementById('search').addEventListener('input',event=>{clearTimeout(timer);let q=event.target.value.trim();if(!q){document.getElementById('results').innerHTML='<div class="empty">Select a graph node or search the vault.</div>';return}timer=setTimeout(()=>search(q),180)});async function search(q){let root=document.getElementById('results');root.classList.add('loading');try{let data=await api('/api/search?q='+encodeURIComponent(q)+'&limit=20');root.replaceChildren(...data.hits.map(hit=>{let b=document.createElement('button');b.className='result';let title=document.createElement('span');let meta=document.createElement('small');escText(title,hit.title);escText(meta,`${hit.kind} · ${hit.privacy} · ${hit.path}`);b.append(title,meta);b.onclick=()=>showResource(hit.content_hash?'raw:'+hit.content_hash:'page:'+hit.path);return b}));if(!data.hits.length)root.innerHTML='<div class="empty">No matching evidence.</div>';document.getElementById('detail').classList.add('open')}catch(error){root.innerHTML='<div class="empty"></div>';escText(root.firstChild,error.message)}finally{root.classList.remove('loading')}}
async function switchView(name){document.querySelectorAll('.view-btn').forEach(button=>button.classList.toggle('active',button.dataset.view===name));let graph=name==='graph';document.getElementById('graphView').classList.toggle('hidden',!graph);document.getElementById('collection').classList.toggle('hidden',graph);if(graph){resize();return}let endpoints={sources:'/api/sources',pages:'/api/pages',timeline:'/api/timeline',integrations:'/api/integrations'};try{let data=state.cache[name]||await api(endpoints[name]);state.cache[name]=data;renderCollection(name,data)}catch(error){renderCollection(name,{error:error.message})}}
function renderCollection(name,data){let title=document.getElementById('collectionTitle'),meta=document.getElementById('collectionMeta'),grid=document.getElementById('collectionGrid');escText(title,{sources:'Sources',pages:'Compiled wiki',timeline:'Timeline',integrations:'Persistent services'}[name]);if(data.error){meta.textContent='';grid.innerHTML='<div class="empty"></div>';escText(grid.firstChild,data.error);return}let items=name==='sources'?data.sources:name==='pages'?data.pages:name==='timeline'?data.events:data.integrations;escText(meta,`${items.length} items${data.redacted?` · ${data.redacted} private`:''}`);grid.replaceChildren(...items.map(item=>{let card=document.createElement('button');card.className='item-card';let heading=document.createElement('b'),info=document.createElement('small'),badge=document.createElement('span');badge.className='badge '+(item.state==='running'||item.state==='ready'||item.freshness==='fresh'?'good':'');let label=item.title||item.label||item.name||item.type;let details=name==='sources'?`${item.branch} · ${item.privacy} · ${item.status}`:name==='pages'?`${item.kind} · ${item.privacy} · ${item.path}`:name==='timeline'?`${item.type} · ${item.detail} · ${String(item.at).slice(0,19)}`:`${item.state} · ${item.managed?'managed':'external'}`;escText(heading,label);escText(info,details);escText(badge,item.freshness||item.state||item.type);card.append(heading,info,badge);let id=item.id;if(id)card.onclick=()=>showResource(id);return card}))}
document.querySelectorAll('.view-btn').forEach(button=>button.onclick=()=>switchView(button.dataset.view));addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key==='k'){e.preventDefault();document.getElementById('search').focus()}});load();
</script>
</body></html>'''
