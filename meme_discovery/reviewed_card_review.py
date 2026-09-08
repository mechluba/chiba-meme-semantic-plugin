"""生成线上卡片格式的可移植人工审核页。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import html
import json


ONLINE_CARD_KEYS = {
    "card_id",
    "canonical_expression",
    "aliases",
    "language_market",
    "semantic_core",
    "culture_scope",
    "serving_scope",
    "age_class",
    "usage_routes",
    "required_context_signals",
    "hard_blocks",
    "positive_contexts",
    "negative_contexts",
    "retrieval_facets",
    "knowledge",
    "serving",
    "human_review",
}


def build_pending_library(document: dict[str, Any]) -> dict[str, Any]:
    cards = [
        item["storage_card"]
        for item in document.get("items") or []
        if item.get("semantic_status") == "pending_human_review" and item.get("storage_card")
    ]
    return {
        "schema_version": 2,
        "library_id": "pending-reviewed-semantic-meme-library-20260902",
        "built_at": datetime.now(timezone.utc).isoformat(),
        "design": {
            "purpose": "由人工初审保留表达生成的线上兼容语义梗卡草稿",
            "runtime_compatible": True,
            "human_review_required": True,
            "serving_note": "human_review.status 为 pending，当前线上运行时不会加载；审核后还需构建向量索引与 release。",
        },
        "card_count": len(cards),
        "cards": cards,
    }


def build_serving_patch(document: dict[str, Any]) -> dict[str, Any]:
    understand_ids = [
        item["storage_card"]["card_id"]
        for item in document.get("items") or []
        if item.get("prior_serving_policy") == "understand" and item.get("storage_card")
    ]
    return {
        "schema_version": 1,
        "status": "pending_human_review",
        "serving": {"understand_only_card_ids": understand_ids},
        "note": "仅为审核建议；最终列表应由页面审核结果导出，不能直接覆盖线上配置。",
    }


def validate_storage_cards(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for item in document.get("items") or []:
        card = item.get("storage_card")
        if not card:
            continue
        card_id = str(card.get("card_id") or "")
        if set(card) != ONLINE_CARD_KEYS:
            errors.append(f"{card_id or item.get('group_id')}: 顶层字段与线上 schema 不一致")
        if not card_id:
            errors.append(f"{item.get('group_id')}: 缺少 card_id")
        elif card_id in seen:
            errors.append(f"{card_id}: card_id 重复")
        seen.add(card_id)
        if card.get("human_review", {}).get("status") != "pending":
            errors.append(f"{card_id}: 未审核卡必须是 pending")
        if item.get("prior_serving_policy") == "understand":
            actions = {
                row.get("expected_action") for row in card.get("positive_contexts") or []
            }
            if actions - {"UNDERSTAND_ONLY"}:
                errors.append(f"{card_id}: 仅理解条目含主动使用正例")
    return errors


def render_review_html(document: dict[str, Any]) -> str:
    payload = json.dumps(document, ensure_ascii=False).replace("</", "<\\/")
    generated = int(document.get("summary", {}).get("generated_card_count") or 0)
    failed = int(document.get("summary", {}).get("failed_card_count") or 0)
    researched = sum(
        bool((item.get("web_research") or {}).get("results"))
        for item in document.get("items") or []
    )
    source_pending = max(generated - researched, 0)
    review_title = html.escape(str(document.get("review_title") or "千叶热梗语义卡二审"))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{review_title}</title>
<style>
:root{{--bg:#f4f5f7;--card:#fff;--line:#dfe3e8;--ink:#17202a;--muted:#68707b;--blue:#2457d6;--green:#147d52;--amber:#a65d00;--red:#b42318}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
header{{position:sticky;top:0;z-index:5;background:#ffffffee;backdrop-filter:blur(12px);border-bottom:1px solid var(--line);padding:12px 18px}}
.top{{display:flex;align-items:center;gap:12px;flex-wrap:wrap;max-width:1500px;margin:auto}} h1{{font-size:18px;margin:0 10px 0 0}} .stat{{color:var(--muted)}}
button,select,input{{font:inherit}} button{{border:1px solid var(--line);background:white;border-radius:7px;padding:6px 10px;cursor:pointer}} button.primary{{background:var(--blue);color:white;border-color:var(--blue)}}
.filters{{max-width:1500px;margin:10px auto 0;display:flex;gap:8px;flex-wrap:wrap}} input[type=search]{{min-width:280px;flex:1;border:1px solid var(--line);border-radius:7px;padding:7px 10px}}
main{{max-width:1500px;margin:14px auto;padding:0 14px 40px;display:grid;gap:10px}}
article{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}} article.invalid{{border-color:var(--red)}}
.row{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}} .grow{{flex:1;min-width:260px}} h2{{font-size:17px;margin:0}} .badge{{display:inline-block;border-radius:999px;background:#eef2f7;color:#445;padding:2px 8px;font-size:12px}} .badge.use{{background:#e7f5ee;color:var(--green)}} .badge.understand{{background:#fff2dc;color:var(--amber)}}
.core{{margin:8px 0 4px}} .muted{{color:var(--muted)}} .routes{{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:7px;margin:8px 0}} .route{{background:#f7f8fa;border-radius:7px;padding:8px}} .route b{{color:var(--blue)}}
.research{{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin:8px 0}} .research>div{{background:#f7f8fa;border-radius:7px;padding:8px}} .source{{margin:7px 0;padding-left:10px;border-left:3px solid var(--line)}} .source a{{color:var(--blue);text-decoration:none}} .source-meta{{font-size:12px;color:var(--muted)}}
.compact{{margin:4px 0;padding-left:20px}} details{{margin-top:8px;border-top:1px dashed var(--line);padding-top:7px}} summary{{cursor:pointer;color:var(--muted);font-weight:600}} textarea{{width:100%;min-height:330px;margin-top:7px;border:1px solid var(--line);border-radius:7px;padding:9px;font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;resize:vertical}}
.json-tools{{display:flex;gap:7px;align-items:center;margin-top:7px}} .error{{color:var(--red);font-size:12px}} .ok{{color:var(--green);font-size:12px}} .empty{{padding:40px;text-align:center;color:var(--muted)}}
.skipped-panel{{max-width:1500px;margin:0 auto 40px;padding:10px 14px;background:white;border:1px solid var(--line);border-radius:10px}} .skipped-list{{margin-top:8px;display:grid;gap:3px;max-height:420px;overflow:auto}} .skipped-row{{display:grid;grid-template-columns:minmax(160px,1fr) minmax(120px,.7fr) minmax(150px,1fr);gap:10px;padding:4px 6px;border-bottom:1px solid #eef0f2}} .skipped-row span{{overflow-wrap:anywhere}}
@media(max-width:700px){{header{{position:static}} .routes,.research{{grid-template-columns:1fr}} input[type=search]{{min-width:100%}}}}
</style>
</head>
<body>
<header><div class="top"><h1>{review_title}</h1><span class="stat">生成 {generated} · 有网络材料 {researched} · 来源待核 {source_pending} · 失败 {failed}</span><span id="progress" class="stat"></span><button class="primary" onclick="exportLibrary()">导出审核后梗库</button><button onclick="exportDecisions()">导出审核记录</button><button onclick="exportServing()">导出仅理解配置</button><button onclick="document.getElementById('importer').click()">导入审核记录</button><input id="importer" type="file" accept="application/json" hidden></div>
<div class="filters"><input id="search" type="search" placeholder="搜索表达、语义、交流意图、圈层、网络材料"><select id="policy"><option value="all">全部策略</option><option value="refine">可使用</option><option value="understand">仅理解</option></select><select id="research"><option value="all">全部检索状态</option><option value="origin_and_current_usage">典故＋近期用法</option><option value="multi_source_support">多源支持</option><option value="encyclopedia_only">仅百科解释</option><option value="current_usage_only">仅近期用法</option><option value="single_web_source">单一网络来源</option><option value="no_reliable_match">来源待核</option></select><select id="decision"><option value="all">全部审核状态</option><option value="pending">待审核</option><option value="approve_use">通过·可使用</option><option value="approve_understand">通过·仅理解</option><option value="reject">淘汰</option></select></div></header>
<main id="cards"></main><details class="skipped-panel" id="skippedPanel"><summary id="skippedSummary"></summary><div class="skipped-list" id="skippedCards"></div></details>
<script id="dataset" type="application/json">{payload}</script>
<script>
const doc=JSON.parse(document.getElementById('dataset').textContent);const KEY='chiba-reviewed-card-decisions-'+(doc.report_kind||'web-grounded-v2')+'-'+(doc.prompt_version||'');
const state=JSON.parse(localStorage.getItem(KEY)||'{{}}');
const items=(doc.items||[]).filter(x=>x.storage_card);const esc=s=>String(s??'').replace(/[&<>\"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[c]));
const safeURL=s=>/^https?:\\/\\//i.test(String(s||''))?String(s):'#';
const researchLabels={{origin_and_current_usage:'典故＋近期用法',multi_source_support:'多源支持',encyclopedia_only:'仅百科解释',current_usage_only:'仅近期用法',single_web_source:'单一网络来源',no_reliable_match:'来源待核'}};
function initialDecision(x){{return x.prior_serving_policy==='understand'?'approve_understand':'approve_use'}}
for(const x of items){{const id=x.storage_card.card_id;if(!state[id])state[id]={{decision:'pending',json:JSON.stringify(x.storage_card,null,2),note:''}}}}
function save(){{localStorage.setItem(KEY,JSON.stringify(state));updateProgress()}}
function parsed(id){{try{{return JSON.parse(state[id].json)}}catch(e){{throw new Error('JSON 解析失败：'+e.message)}}}}
function validateCard(card){{const required=['card_id','canonical_expression','aliases','language_market','semantic_core','culture_scope','serving_scope','age_class','usage_routes','required_context_signals','hard_blocks','positive_contexts','negative_contexts','retrieval_facets','knowledge','serving','human_review'];const missing=required.filter(k=>!(k in card));if(missing.length)throw new Error('缺少字段：'+missing.join('、'));if(!Array.isArray(card.usage_routes)||!card.usage_routes.length)throw new Error('usage_routes 不能为空');return card}}
function researchHTML(x){{const w=x.web_research||{{}},k=x.storage_card?.knowledge?.source_material?.web_research||{{}},sources=w.results||[],questions=w.question_queries||[];const list=sources.map(v=>`<div class="source"><a href="${{esc(safeURL(v.url))}}" target="_blank" rel="noopener noreferrer">${{esc(v.title||v.url)}}</a><div class="source-meta">${{esc(v.provider==='so_qa'?'通用问句检索':v.provider)}}${{v.published_at?' · '+esc(v.published_at.slice(0,10)):''}} · ${{esc(v.match_quality||'')}}</div>${{v.snippet?`<div>${{esc(v.snippet)}}</div>`:''}}</div>`).join('');const queryLine=questions.length?`<div class="muted">检索问句：${{esc(questions.join('；'))}}</div>`:'';return `<div class="research"><div><b>典故 / 来源</b><div>${{esc(k.origin_summary||'来源待核')}}</div></div><div><b>当前用法</b><div>${{esc(k.current_usage_summary||'待核')}}</div></div></div><div class="muted">检索：${{esc(researchLabels[w.status]||w.status||'来源待核')}} · 时效：${{esc(k.freshness_assessment||w.freshness?.class||'unknown')}} · 可信度：${{esc(k.research_confidence||'low')}}</div>${{queryLine}}<details><summary>互联网检索材料 ${{sources.length}} 条</summary>${{list||'<div class="muted">未找到可靠外部命中，仅保留弹幕证据，需人工补充来源。</div>'}}</details>`}}
function cardHTML(x){{const c=x.storage_card,s=state[c.card_id],routes=(c.usage_routes||[]).map(r=>`<div class="route"><b>${{esc(r.route_tag)}}</b><div>${{esc(r.when)}}</div><div class="muted">交流意图：${{esc(r.communicative_intent)}}</div><div class="muted">用法：${{esc((r.allowed_realizations||[]).join(' / '))}}</div></div>`).join(''),firstReview=x.first_review?'<span class="badge">一审淘汰</span>':'',policyLabel=x.prior_serving_policy==='understand'?(x.first_review?'原初仅理解':'初审仅理解'):(x.first_review?'原初可使用':'初审可使用'),suggestionLabel=x.first_review?'采用原初策略':'采用初审建议';return `<article id="a-${{c.card_id}}" data-policy="${{x.prior_serving_policy}}" data-decision="${{s.decision}}"><div class="row"><div class="grow"><h2>${{esc(c.canonical_expression)}} <span class="badge">${{esc(x.content_type)}}</span> ${{firstReview}} <span class="badge ${{x.prior_serving_policy==='understand'?'understand':'use'}}">${{esc(policyLabel)}}</span></h2><div class="muted">${{esc((c.aliases||[]).join(' · '))}}</div></div><select aria-label="审核决定" onchange="setDecision('${{c.card_id}}',this.value)"><option value="pending" ${{s.decision==='pending'?'selected':''}}>待审核</option><option value="approve_use" ${{s.decision==='approve_use'?'selected':''}}>通过·可使用</option><option value="approve_understand" ${{s.decision==='approve_understand'?'selected':''}}>通过·仅理解</option><option value="reject" ${{s.decision==='reject'?'selected':''}}>淘汰</option></select><button onclick="acceptSuggestion('${{c.card_id}}','${{initialDecision(x)}}')">${{esc(suggestionLabel)}}</button></div><div class="core"><b>真实表达：</b>${{esc(c.semantic_core)}}</div><div class="muted">圈层：${{esc(c.culture_scope)}} · 投放范围：${{esc(c.serving_scope)}}</div>${{researchHTML(x)}}<div class="routes">${{routes}}</div><div class="row"><div class="grow"><b>触发信号：</b>${{esc((c.required_context_signals||[]).join('；'))}}</div><div class="grow"><b>硬禁用：</b>${{esc((c.hard_blocks||[]).join('；'))}}</div></div><details><summary>存储内容 JSON（已折叠，可直接编辑）</summary><textarea spellcheck="false" oninput="editJson('${{c.card_id}}',this.value)" onblur="checkJson('${{c.card_id}}')">${{esc(s.json)}}</textarea><div class="json-tools"><button onclick="formatJson('${{c.card_id}}')">格式化并校验</button><button onclick="resetJson('${{c.card_id}}')">恢复模型版本</button><span id="msg-${{c.card_id}}"></span></div></details></article>`}}
function render(){{const q=document.getElementById('search').value.trim().toLowerCase(),p=document.getElementById('policy').value,r=document.getElementById('research').value,d=document.getElementById('decision').value;const shown=items.filter(x=>{{const c=x.storage_card,s=state[c.card_id],w=x.web_research||{{}};return(p==='all'||x.prior_serving_policy===p)&&(r==='all'||w.status===r)&&(d==='all'||s.decision===d)&&(!q||JSON.stringify(x).toLowerCase().includes(q))}});document.getElementById('cards').innerHTML=shown.length?shown.map(cardHTML).join(''):'<div class="empty">没有符合条件的卡片</div>';updateProgress()}}
function setDecision(id,v){{state[id].decision=v;save();const a=document.getElementById('a-'+id);if(a)a.dataset.decision=v;if(document.getElementById('decision').value!=='all')render()}}
function acceptSuggestion(id,v){{state[id].decision=v;save();render()}}
function editJson(id,v){{state[id].json=v;save();const m=document.getElementById('msg-'+id);if(m)m.textContent='已暂存'}}
function checkJson(id){{const m=document.getElementById('msg-'+id),a=document.getElementById('a-'+id);try{{validateCard(parsed(id));m.className='ok';m.textContent='JSON 有效';a.classList.remove('invalid')}}catch(e){{m.className='error';m.textContent=e.message;a.classList.add('invalid')}}}}
function formatJson(id){{const c=validateCard(parsed(id));state[id].json=JSON.stringify(c,null,2);save();render();setTimeout(()=>{{const a=document.getElementById('a-'+id);if(a)a.querySelector('details').open=true}},0)}}
function resetJson(id){{const x=items.find(x=>x.storage_card.card_id===id);state[id].json=JSON.stringify(x.storage_card,null,2);save();render()}}
function updateProgress(){{const counts={{pending:0,approve_use:0,approve_understand:0,reject:0}};items.forEach(x=>counts[state[x.storage_card.card_id].decision]++);document.getElementById('progress').textContent=`待审 ${{counts.pending}} · 可用 ${{counts.approve_use}} · 仅理解 ${{counts.approve_understand}} · 淘汰 ${{counts.reject}}`}}
function renderSkipped(){{const rows=doc.skipped_candidates||[],panel=document.getElementById('skippedPanel');panel.hidden=!rows.length;if(!rows.length)return;document.getElementById('skippedSummary').textContent=`未生成可入库梗卡（${{rows.length}}，点击展开）`;document.getElementById('skippedCards').innerHTML=rows.map(x=>`<div class="skipped-row"><span><b>${{esc(x.phrase||x.candidate_id)}}</b></span><span>${{esc(x.reason||'unknown')}}</span><span>${{esc(x.source_kind||'')}} / ${{esc(x.run_id||'')}}</span></div>`).join('')}}
function download(name,value){{const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)+'\\n'],{{type:'application/json'}}));a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)}}
function reviewedCards(){{const out=[];for(const x of items){{const id=x.storage_card.card_id,s=state[id];if(s.decision==='reject'||s.decision==='pending')continue;const c=validateCard(parsed(id));c.human_review={{status:'approved',note:s.decision==='approve_understand'?'二审通过：仅理解':'二审通过：可在语义门控下使用',updated_at:new Date().toISOString()}};out.push(c)}}return out}}
function exportLibrary(){{try{{const cards=reviewedCards();download('reviewed-semantic-meme-library.json',{{schema_version:2,library_id:'reviewed-semantic-meme-library-20260902',built_at:new Date().toISOString(),design:{{purpose:'人工二审后的线上兼容语义梗卡；仍需构建向量索引与 release'}},card_count:cards.length,cards}})}}catch(e){{alert(e.message)}}}}
function exportServing(){{try{{const ids=[];for(const x of items){{const id=x.storage_card.card_id,s=state[id];if(s.decision==='approve_understand'){{validateCard(parsed(id));ids.push(id)}}}}download('reviewed-understand-only-config-patch.json',{{schema_version:1,serving:{{understand_only_card_ids:ids}}}})}}catch(e){{alert(e.message)}}}}
function exportDecisions(){{download('meme-semantic-card-review-decisions.json',{{schema_version:1,exported_at:new Date().toISOString(),source_prompt_version:doc.prompt_version,decisions:Object.fromEntries(items.map(x=>{{const id=x.storage_card.card_id;return[id,{{...state[id],canonical_expression:x.storage_card.canonical_expression}}]}}))}})}}
document.getElementById('importer').onchange=async e=>{{try{{const v=JSON.parse(await e.target.files[0].text()),incoming=v.decisions||v;for(const [id,row] of Object.entries(incoming))if(state[id])state[id]={{...state[id],...row}};save();render()}}catch(err){{alert('导入失败：'+err.message)}}e.target.value=''}};
document.getElementById('search').oninput=render;document.getElementById('policy').onchange=render;document.getElementById('research').onchange=render;document.getElementById('decision').onchange=render;save();render();renderSkipped();
</script>
</body></html>"""


def html_escape_json(value: Any) -> str:
    """保留给调用方做小片段渲染。"""
    return html.escape(json.dumps(value, ensure_ascii=False, indent=2))
