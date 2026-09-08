#!/usr/bin/env python3
"""把真实用户末轮的完整链路回放 JSON 渲染成可独立传输的审核页。"""

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
    response_changed_count = sum(
        bool((case.get("replay") or {}).get("changed")) for case in cases
    )
    title = "新版梗库 · 生产用户轮次完整链路回放"
    data = _json_for_script(payload)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root{{--bg:#0c0d10;--panel:#15171c;--panel2:#1c1f26;--line:#2b3039;--text:#edf0f5;--muted:#98a2b2;--green:#66d79e;--yellow:#f0c96e;--red:#ff8181;--blue:#78b9ff}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.52 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
.wrap{{max-width:1580px;margin:auto;padding:18px}}h1{{font-size:24px;margin:0 0 3px}}.muted{{color:var(--muted)}}code{{font:12px ui-monospace,SFMono-Regular,Menlo,monospace}}
.summary{{display:grid;grid-template-columns:repeat(8,minmax(105px,1fr));gap:7px;margin:13px 0}}.metric{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:9px 11px}}.metric b{{display:block;font-size:22px;line-height:1.1}}.metric span{{color:var(--muted);font-size:11px}}
.notice{{border:1px solid #334457;background:#151c24;border-radius:8px;padding:9px 11px;margin:8px 0}}.notice.warn{{border-color:#5a4930;background:#211d15}}
.toolbar{{position:sticky;top:0;z-index:5;display:flex;gap:7px;align-items:center;flex-wrap:wrap;background:rgba(12,13,16,.96);padding:9px 0;border-bottom:1px solid var(--line)}}
input,select,button{{background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:7px;padding:7px 9px}}input{{min-width:260px;flex:1}}button{{cursor:pointer}}.count{{margin-left:auto;color:var(--muted)}}
.case{{background:var(--panel);border:1px solid var(--line);border-radius:9px;margin:8px 0;overflow:hidden}}.head{{display:grid;grid-template-columns:62px 152px 142px 94px 118px 1fr;gap:8px;align-items:center;padding:8px 10px;background:var(--panel2)}}.id{{font-weight:750}}
.tag{{display:inline-flex;border:1px solid var(--line);border-radius:999px;padding:1px 7px;font-size:11px;color:var(--muted)}}.tag.new{{color:var(--blue);border-color:#355b7c}}.tag.use{{color:var(--green);border-color:#315e49}}.tag.no{{color:var(--yellow);border-color:#66562e}}
.body{{padding:9px 10px}}.candidates{{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:7px}}.planner-note{{color:#c8d0dc;margin:5px 0}}.compare{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}.bubble{{background:#101217;border:1px solid var(--line);border-radius:7px;padding:8px;white-space:pre-wrap;min-height:54px}}.label{{font-size:11px;color:var(--muted);margin-bottom:3px}}
details{{border-top:1px solid var(--line)}}details>summary{{cursor:pointer;padding:7px 10px;color:#c8d0dc;list-style:none}}details>summary:before{{content:"＋";display:inline-block;width:18px;color:var(--muted)}}details[open]>summary:before{{content:"－"}}.context{{padding:0 10px 9px}}.line{{display:grid;grid-template-columns:72px 1fr;gap:7px;padding:3px 0;border-bottom:1px dashed #252a33}}.role{{color:var(--blue);font-size:11px}}.role.assistant{{color:var(--green)}}
.json-grid{{display:grid;grid-template-columns:1fr 1fr;gap:7px;padding:0 9px 9px}}.json-box{{min-width:0}}pre{{margin:0;max-height:520px;overflow:auto;background:#090a0d;border:1px solid #242832;border-radius:7px;padding:8px;color:#d0d7e5;font:11px/1.43 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;word-break:break-word}}footer{{color:var(--muted);font-size:12px;margin:16px 0}}
@media(max-width:920px){{.summary{{grid-template-columns:repeat(2,1fr)}}.head{{grid-template-columns:60px 1fr}}.compare,.json-grid{{grid-template-columns:1fr}}}}
</style></head><body><main class="wrap">
<h1>{title}</h1>
<div class="muted">生产 revision <code>{html.escape(str(payload['deployed_revision']))}</code> · 梗包 <code>{html.escape(str(release['release_id']))}</code> · {html.escape(str(payload['created_at']))}</div>
<section class="summary">
 <div class="metric"><b>{summary['case_count']}</b><span>有效真实用户轮次</span></div>
 <div class="metric"><b>{summary['success_count']}</b><span>成功完成回放</span></div>
 <div class="metric"><b>{summary['planner_reply_count']}</b><span>Planner 选择回复</span></div>
 <div class="metric"><b>{summary['planner_non_reply_count']}</b><span>Planner 本步继续规划</span></div>
 <div class="metric"><b>{summary['candidate_new_card_count']}</b><span>Top-3 含新增卡</span></div>
 <div class="metric"><b>{summary['selected_new_card_count']}</b><span>最终选中新增卡</span></div>
 <div class="metric"><b>{summary['new_meme_used_count']}</b><span>回复实际使用新增梗</span></div>
 <div class="metric"><b>{response_changed_count}</b><span>重跑可见回复变化</span></div>
</section>
<div class="notice"><b>链路结论：</b>本批先由 Planner 对真实用户末轮重新决策；选择 reply 后，Planner 省略或 SKIP 梗参数的轮次继续经过插件同款语义裁判，再决定是否向 Replyer 注入。每条案例均保留 Planner、裁判与 Replyer 的请求/返回证据。</div>
<div class="notice warn"><b>样本与边界：</b>{html.escape(method['sample'])} {html.escape(method['caveat'])}</div>
<div class="toolbar">
 <input id="search" placeholder="搜索用户消息、候选梗、Planner 或裁判理由">
 <select id="reply"><option value="">全部 Planner 动作</option><option value="yes">进入 reply</option><option value="no">未进入 reply</option></select>
 <select id="action"><option value="">全部梗动作</option><option>USE</option><option>UNDERSTAND_ONLY</option><option>SKIP</option><option>PLANNER_CONTINUE</option></select>
 <select id="source"><option value="">全部决策来源</option><option>planner</option><option>semantic_selector</option><option>semantic_selector_skip</option><option>none</option></select>
 <select id="candidate"><option value="">全部候选</option><option value="new">Top-3 含新增卡</option><option value="old">Top-3 不含新增卡</option></select>
 <button id="expand">展开全部 JSON</button><button id="collapse">折叠全部 JSON</button><span class="count" id="count"></span>
</div><section id="cases"></section>
<footer>{html.escape(method['source'])}；{html.escape(method['replay'])} 请求与返回 JSON 已内嵌，页面不依赖本机目录或外部 JSON。</footer>
</main><script id="report-data" type="application/json">{data}</script><script>
const REPORT=JSON.parse(document.getElementById('report-data').textContent);
const root=document.getElementById('cases'),search=document.getElementById('search'),reply=document.getElementById('reply'),action=document.getElementById('action'),source=document.getElementById('source'),candidate=document.getElementById('candidate'),count=document.getElementById('count');
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));const pretty=x=>JSON.stringify(x,null,2);const box=(label,value)=>`<div class="json-box"><div class="label">${{esc(label)}}</div><pre>${{esc(pretty(value))}}</pre></div>`;
function searchable(c){{return JSON.stringify({{id:c.case_id,context:c.context,candidates:c.candidates,planner:c.planner?.response,selector:c.selector,selection:c.selection,old:c.historical?.visible_text,new:c.replay?.visible_text}}).toLowerCase()}}
function renderCase(c){{
 const sel=c.selection||{{action:'ERROR',source:'none'}},planner=c.planner||{{}},cands=c.candidates||[],topNew=cands.some(x=>x.is_new_card),didReply=!!planner.reply_selected;
 const candHtml=cands.map(x=>`<span class="tag ${{x.is_new_card?'new':''}}">${{esc(x.expression)}} · ${{Number(x.similarity).toFixed(3)}}${{x.is_new_card?' · 新':''}}</span>`).join('');
 const lines=(c.context||[]).map(x=>`<div class="line"><span class="role ${{x.role}}">${{esc(x.role)}}</span><span>${{esc(x.text)}}</span></div>`).join('');
 const attempts=(c.replay?.replyer_attempts||[]).flatMap(x=>[box(`Replyer 回放请求 · attempt ${{x.attempt}}`,x.request),box(`Replyer 回放返回 · attempt ${{x.attempt}}`,x.response)]).join('');
 const plannerText=planner.reply_selected?`Planner reply · 梗参数 ${{planner.requested_meme_action||'OMITTED'}}`:`Planner 本步未直接 reply · ${{(planner.response?.tool_calls||[]).map(x=>x.function?.name).filter(Boolean).join(', ')||'无工具'}}`;
 const selectorText=c.selector?`；裁判 ${{c.selector.requested_action}}：${{c.selector.reason||''}}`:'';
 return `<article class="case" data-search="${{esc(searchable(c))}}" data-reply="${{didReply?'yes':'no'}}" data-action="${{esc(sel.action)}}" data-source="${{esc(sel.source)}}" data-new="${{topNew?'new':'old'}}">
 <div class="head"><span class="id">${{esc(c.case_id)}}</span><span>${{esc(c.created_at)}}</span><span>${{esc(c.session)}}</span><span>${{Number(c.context_age_seconds).toFixed(2)}}s</span><span class="tag ${{didReply?'use':'no'}}">${{didReply?'reply':'继续规划'}}</span><span><span class="tag ${{sel.action==='USE'?'use':sel.action==='PLANNER_CONTINUE'?'no':''}}">${{esc(sel.action)}}</span> ${{sel.expression?`<span class="tag new">${{esc(sel.expression)}}</span>`:''}} · ${{esc(sel.source)}}</span></div>
 <div class="body"><div class="candidates">${{candHtml}}</div><div class="planner-note">${{esc(plannerText+selectorText)}}</div><div class="compare"><div><div class="label">历史真实回复</div><div class="bubble">${{esc(c.historical?.visible_text||'')}}</div></div><div><div class="label">完整链路回放${{didReply?'':'（Planner 未授权回复）'}}</div><div class="bubble">${{esc(c.replay?.visible_text||'')}}</div></div></div></div>
 <details><summary>最近 6 条数据库消息</summary><div class="context">${{lines}}</div></details>
 <details class="json"><summary>Planner 请求与返回 JSON</summary><div class="json-grid">${{box('Planner 请求',planner.request)}}${{box('Planner 返回',planner.response)}}</div></details>
 <details class="json"><summary>召回与语义裁判 JSON</summary><div class="json-grid">${{box('Embedding 与候选',{{embedding:c.embedding,candidates:c.candidates}})}}${{box('语义裁判',c.selector)}}</div></details>
 <details class="json"><summary>历史 Replyer 请求与返回 JSON</summary><div class="json-grid">${{box('历史 Replyer 请求',c.historical?.replyer_request)}}${{box('历史 Replyer 返回',c.historical?.replyer_response)}}</div></details>
 <details class="json"><summary>回放 Replyer 与效果评估 JSON</summary><div class="json-grid">${{attempts}}${{box('仅理解质量门',c.understand_only_gate)}}${{box('USE 效果评估',c.effect)}}</div></details></article>`;
}}
root.innerHTML=REPORT.cases.map(renderCase).join('');
function apply(){{const q=search.value.trim().toLowerCase();let n=0;document.querySelectorAll('.case').forEach(el=>{{const ok=(!q||el.dataset.search.includes(q))&&(!reply.value||el.dataset.reply===reply.value)&&(!action.value||el.dataset.action===action.value)&&(!source.value||el.dataset.source===source.value)&&(!candidate.value||el.dataset.new===candidate.value);el.hidden=!ok;if(ok)n++}});count.textContent=`${{n}} / ${{REPORT.cases.length}}`;}}
[search,reply,action,source,candidate].forEach(el=>el.addEventListener(el===search?'input':'change',apply));document.getElementById('expand').onclick=()=>document.querySelectorAll('details.json').forEach(x=>x.open=true);document.getElementById('collapse').onclick=()=>document.querySelectorAll('details.json').forEach(x=>x.open=false);apply();
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
