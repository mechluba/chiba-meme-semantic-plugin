#!/usr/bin/env python3
"""把生产历史梗库回放 JSON 渲染为可独立传输的单文件审核页。"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from typing import Any

import html
import json


def _json_for_script(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace(
        "</script", "<\\/script"
    )


def render(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    release = payload["release"]
    method = payload["method"]
    cases = payload["cases"]
    request_changed_count = sum(
        case["historical"]["request"]
        != case["replay"]["attempts"][0]["request"]
        for case in cases
    )
    response_changed_count = sum(
        case["historical"]["visible_text"] != case["replay"]["visible_text"]
        for case in cases
    )
    title = "新版梗库 · 生产历史消息回放"
    data = _json_for_script(payload)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root{{--bg:#0c0d10;--panel:#15171c;--panel2:#1c1f26;--line:#2b3039;--text:#eceff4;--muted:#9aa3b2;--green:#62d69c;--yellow:#f3c969;--red:#ff7d7d;--blue:#75b7ff}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
.wrap{{max-width:1540px;margin:auto;padding:20px}} h1{{font-size:25px;margin:0 0 3px}} h2{{font-size:15px;margin:0}} .muted{{color:var(--muted)}}
.summary{{display:grid;grid-template-columns:repeat(8,minmax(110px,1fr));gap:8px;margin:14px 0}} .metric{{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:10px 12px}} .metric b{{display:block;font-size:23px;line-height:1.1}} .metric span{{color:var(--muted);font-size:12px}}
.notice{{background:#171d1b;border:1px solid #29483b;border-radius:9px;padding:10px 12px;margin:10px 0}} .notice.warn{{background:#211d14;border-color:#554722}}
.toolbar{{position:sticky;top:0;z-index:4;display:flex;gap:8px;align-items:center;flex-wrap:wrap;background:rgba(12,13,16,.96);padding:10px 0;border-bottom:1px solid var(--line)}}
input,select,button{{background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:7px;padding:7px 9px}} input{{min-width:280px;flex:1}} button{{cursor:pointer}} .count{{margin-left:auto;color:var(--muted)}}
.case{{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin:9px 0;overflow:hidden}} .case-head{{display:grid;grid-template-columns:72px 155px 145px 105px 1fr;gap:9px;align-items:center;padding:9px 11px;background:var(--panel2)}}
.case-id{{font-weight:750}} .tag{{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:999px;padding:1px 7px;font-size:11px;color:var(--muted)}} .tag.new{{color:var(--blue);border-color:#355779}} .tag.use{{color:var(--green);border-color:#315e49}} .tag.skip{{color:var(--muted)}}
.body{{padding:10px 11px}} .compare{{display:grid;grid-template-columns:1fr 1fr;gap:9px}} .bubble{{border:1px solid var(--line);border-radius:8px;padding:9px;background:#111318;white-space:pre-wrap;min-height:58px}} .label{{font-size:11px;color:var(--muted);margin-bottom:4px;text-transform:uppercase;letter-spacing:.05em}}
.candidates{{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}} .reason{{color:#c7ced9;margin:6px 0}} details{{border-top:1px solid var(--line)}} details>summary{{cursor:pointer;padding:8px 11px;color:#c8d0dc;list-style:none}} details>summary::-webkit-details-marker{{display:none}} details>summary:before{{content:"＋";display:inline-block;width:18px;color:var(--muted)}} details[open]>summary:before{{content:"－"}}
.json-grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:0 10px 10px}} .json-box{{min-width:0}} pre{{margin:0;max-height:520px;overflow:auto;background:#090a0d;border:1px solid #242832;border-radius:7px;padding:9px;color:#cfd6e4;font:11px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;word-break:break-word}}
.context{{padding:0 11px 9px}} .line{{display:grid;grid-template-columns:72px 1fr;gap:7px;padding:3px 0;border-bottom:1px dashed #242832}} .role{{color:var(--blue);font-size:11px}} .role.assistant{{color:var(--green)}}
.empty{{padding:40px;text-align:center;color:var(--muted)}} footer{{color:var(--muted);font-size:12px;margin:18px 0}} @media(max-width:900px){{.summary{{grid-template-columns:repeat(2,1fr)}}.case-head{{grid-template-columns:70px 1fr}}.compare,.json-grid{{grid-template-columns:1fr}}}}
</style>
</head>
<body><main class="wrap">
<h1>{title}</h1>
<div class="muted">生产 revision <code>{html.escape(str(payload['deployed_revision']))}</code> · 梗包 <code>{html.escape(str(release['release_id']))}</code> · {html.escape(str(payload['created_at']))}</div>
<section class="summary">
  <div class="metric"><b>{summary['case_count']}</b><span>历史请求样本</span></div>
  <div class="metric"><b>{summary['success_count']}</b><span>成功回放</span></div>
  <div class="metric"><b>{summary['candidate_new_card_count']}</b><span>Top-3 含新增卡</span></div>
  <div class="metric"><b>{summary['selected_new_card_count']}</b><span>裁判选中新增卡</span></div>
  <div class="metric"><b>{summary['new_meme_used_count']}</b><span>最终实际用了新增梗</span></div>
  <div class="metric"><b>{summary['action_counts']['SKIP']}</b><span>裁判 SKIP</span></div>
  <div class="metric"><b>{request_changed_count}</b><span>请求实际注入梗资源</span></div>
  <div class="metric"><b>{response_changed_count}</b><span>模型重跑回复变化</span></div>
</section>
<div class="notice"><b>结论：</b>本批 200 条中，新卡大量进入语义召回，但没有一条通过语义许可，因此 200 条 Replyer 请求都没有注入梗资源。重跑回复虽然全部发生变化，但属于同请求下的模型随机性，不能算新增梗效果。页面完整保留每条证据，便于检查是语料不匹配、路线过窄，还是裁判过于保守。</div>
<div class="notice warn"><b>边界：</b>{html.escape(method['sample'])}。{html.escape(method['caveat'])} 请求与返回 JSON 均内嵌在本 HTML；身份标识已匿名化，模型内部推理已移除。</div>
<div class="toolbar">
  <input id="search" placeholder="搜索案例、消息、候选梗或裁判理由">
  <select id="action"><option value="">全部动作</option><option>USE</option><option>UNDERSTAND_ONLY</option><option>SKIP</option></select>
  <select id="candidate"><option value="">全部候选</option><option value="new">Top-3 含新增卡</option><option value="old">Top-3 不含新增卡</option></select>
  <select id="changed"><option value="">全部回复</option><option value="yes">回复有变化</option><option value="no">回复无变化</option></select>
  <button id="expand">展开全部 JSON</button><button id="collapse">折叠全部 JSON</button>
  <span class="count" id="count"></span>
</div>
<section id="cases"></section>
<footer>{html.escape(method['source'])}；{html.escape(method['replay'])}。完整 JSON 同目录另存，HTML 本身不依赖外部文件。</footer>
</main>
<script id="report-data" type="application/json">{data}</script>
<script>
const REPORT=JSON.parse(document.getElementById('report-data').textContent);
const root=document.getElementById('cases'), search=document.getElementById('search'), action=document.getElementById('action'), candidate=document.getElementById('candidate'), changed=document.getElementById('changed'), count=document.getElementById('count');
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
const pretty=x=>JSON.stringify(x,null,2);
const jsonBox=(label,value)=>`<div class="json-box"><div class="label">${{esc(label)}}</div><pre>${{esc(pretty(value))}}</pre></div>`;
function caseSearch(c){{return JSON.stringify({{id:c.case_id,context:c.context,candidates:c.candidates,selection:c.selection,reason:c.selector?.reason,old:c.historical?.visible_text,new:c.replay?.visible_text}}).toLowerCase()}}
function renderCase(c){{
 const sel=c.selection||{{action:'ERROR'}}, candidates=c.candidates||[], topNew=candidates.some(x=>x.is_new_card), result=c.result||{{}}, replayReq=c.replay?.attempts?.[0]?.request||null, histReq=c.historical?.request||null, requestChanged=pretty(replayReq)!==pretty(histReq);
 const cand=candidates.map(x=>`<span class="tag ${{x.is_new_card?'new':''}}">${{esc(x.expression)}} · ${{Number(x.similarity).toFixed(3)}}${{x.is_new_card?' · 新':''}}</span>`).join('');
 const lines=(c.context||[]).map(x=>`<div class="line"><span class="role ${{x.role}}">${{esc(x.role)}}</span><span>${{esc(x.text)}}</span></div>`).join('');
 const attempts=(c.replay?.attempts||[]).flatMap(x=>[jsonBox(`回放请求 JSON · attempt ${{x.attempt}}`,x.request),jsonBox(`回放返回 JSON · attempt ${{x.attempt}}`,x.response)]).join('');
 const auxiliary=[['Embedding 请求/返回',c.embedding],['语义裁判请求/返回',c.selector],['仅理解质量门',c.understand_only_gate],['USE 效果评估',c.effect]].filter(x=>x[1]).map(x=>jsonBox(x[0],x[1])).join('');
 return `<article class="case" data-search="${{esc(caseSearch(c))}}" data-action="${{esc(sel.action)}}" data-new="${{topNew?'new':'old'}}" data-changed="${{c.replay?.changed?'yes':'no'}}">
 <div class="case-head"><span class="case-id">${{esc(c.case_id)}}</span><span>${{esc(c.created_at)}}</span><span>${{esc(c.session)}}</span><span>${{Number(c.context_age_seconds).toFixed(2)}}s</span><span><span class="tag ${{sel.action==='USE'?'use':'skip'}}">${{esc(sel.action)}}</span> ${{sel.expression?`<span class="tag new">${{esc(sel.expression)}}</span>`:''}}</span></div>
 <div class="body"><div class="candidates">${{cand}}</div><div class="reason">裁判：${{esc(c.selector?.reason||c.error||'—')}}</div><div class="compare"><div><div class="label">历史回复</div><div class="bubble">${{esc(c.historical?.visible_text||'')}}</div></div><div><div class="label">新版回放 · ${{requestChanged?'请求有梗库改动':'请求相同/无梗注入'}}</div><div class="bubble">${{esc(c.replay?.visible_text||'')}}</div></div></div></div>
 <details><summary>最近 6 条数据库消息</summary><div class="context">${{lines}}</div></details>
 <details class="json"><summary>历史请求与返回 JSON</summary><div class="json-grid">${{jsonBox('历史请求 JSON',histReq)}}${{jsonBox('历史返回 JSON',c.historical?.response)}}</div></details>
 <details class="json"><summary>新版回放请求与返回 JSON</summary><div class="json-grid">${{attempts}}</div></details>
 <details class="json"><summary>召回、裁判与评估 JSON</summary><div class="json-grid">${{auxiliary}}</div></details>
 </article>`;
}}
root.innerHTML=REPORT.cases.map(renderCase).join('');
function apply(){{const q=search.value.trim().toLowerCase();let shown=0;document.querySelectorAll('.case').forEach(el=>{{const ok=(!q||el.dataset.search.includes(q))&&(!action.value||el.dataset.action===action.value)&&(!candidate.value||el.dataset.new===candidate.value)&&(!changed.value||el.dataset.changed===changed.value);el.hidden=!ok;if(ok)shown++}});count.textContent=`${{shown}} / ${{REPORT.cases.length}}`;}}
[search,action,candidate,changed].forEach(el=>el.addEventListener(el===search?'input':'change',apply));
document.getElementById('expand').onclick=()=>document.querySelectorAll('details.json').forEach(x=>x.open=true);
document.getElementById('collapse').onclick=()=>document.querySelectorAll('details.json').forEach(x=>x.open=false);
apply();
</script></body></html>"""


def main() -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("输入 JSON 顶层必须是对象")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(payload), encoding="utf-8")
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
