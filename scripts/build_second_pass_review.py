#!/usr/bin/env python3
"""根据初审决定生成“内容类型 × 使用策略 × 合并关系”的二审页面。"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import argparse
import hashlib
import json


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUMMARY = (
    REPO_ROOT
    / "out"
    / "p0-meme-discovery"
    / "reports"
    / "20260827-20260831"
    / "multi-day-summary.json"
)


MERGE_PROPOSALS = [
    {
        "canonical": "我去，不早说",
        "members": ["我去，不早说", "我去不早说"],
        "relation": "orthographic_variant",
        "reason": "标点与分词变体，表达和语用相同。",
        "content_type": "meme",
    },
    {
        "canonical": "是啊，吃什么",
        "members": ["是啊，吃什么", "是啊 吃什么"],
        "relation": "orthographic_variant",
        "reason": "仅标点不同。",
        "content_type": "meme",
    },
    {
        "canonical": "懂你意思",
        "members": ["懂你意思", "东尼意思"],
        "relation": "homophone_variant",
        "reason": "“东尼意思”是“懂你意思”的谐音写法。",
        "content_type": "meme",
    },
    {
        "canonical": "牛来",
        "members": ["牛来", "牛来了"],
        "relation": "grammatical_variant",
        "reason": "完成体助词变体，指向同一表达。",
        "content_type": "meme",
    },
    {
        "canonical": "多少？（夺少体）",
        "members": ["多少？", "夺少？"],
        "relation": "homophone_variant",
        "reason": "“夺少”是“多少”的谐音强调写法。",
        "content_type": "meme",
    },
    {
        "canonical": "大胆（big胆体）",
        "members": ["大胆", "big胆！"],
        "relation": "bilingual_homophone_variant",
        "reason": "中英混写的谐音/替换变体。",
        "content_type": "meme",
    },
    {
        "canonical": "设备玩耍 / device play",
        "members": ["设备玩耍", "device play"],
        "relation": "translation_variant",
        "reason": "中英文直译镜像，当前证据指向同一直播圈表达。",
        "content_type": "meme",
    },
    {
        "canonical": "那么，代价是什么呢",
        "members": ["那么代价呢", "那么，代价是什么呢"],
        "relation": "synonym_variant",
        "reason": "省略句与完整句变体。",
        "content_type": "meme",
    },
    {
        "canonical": "你们不要再打了啦",
        "members": ["你们不要再打了啦", "你们不要再吵了啦"],
        "relation": "template_variant",
        "reason": "同一劝架名场面句式的动词替换。",
        "content_type": "meme",
    },
    {
        "canonical": "垂死病中惊坐起",
        "members": ["垂死病中惊坐起", "垂死梦中惊坐起"],
        "relation": "misquote_variant",
        "reason": "固定诗句改写/误引变体。",
        "content_type": "meme",
    },
    {
        "canonical": "再见了，月小弟",
        "members": ["再见了，月小弟", "再见了月小弟"],
        "relation": "orthographic_variant",
        "reason": "仅标点不同。",
        "content_type": "meme",
    },
    {
        "canonical": "R.I.P.",
        "members": ["R.I.P.", "rip"],
        "relation": "case_punctuation_variant",
        "reason": "大小写和标点变体。",
        "content_type": "catchphrase",
    },
    {
        "canonical": "绷不住了",
        "members": ["没绷住", "绷不住了", "难绷", "这谁能绷住？", "蚌埠住了", "绷不住", "这谁绷得住"],
        "relation": "meme_family",
        "reason": "同义、谐音与反问句式构成同一“绷”系表达族；具体路线可在语义提炼时拆分。",
        "content_type": "meme",
    },
    {
        "canonical": "绷住（反向句式）",
        "members": ["绷住", "轻松绷住。"],
        "relation": "template_variant",
        "reason": "“绷不住”家族的反向/反讽句式，先独立于主族保留。",
        "content_type": "meme",
    },
    {
        "canonical": "中国人会飞（身份模板）",
        "members": ["中国人会飞", "中国人能飞", "藏剑人会飞（）"],
        "relation": "template_family",
        "reason": "“{身份/群体}人会飞”句式模板及同义动词变体。",
        "content_type": "meme",
    },
    {
        "canonical": "不对（拆字/谐音体）",
        "members": ["不兑", "补兑！", "又寸"],
        "relation": "homophone_character_split",
        "reason": "“不对”的谐音和汉字拆分写法。",
        "content_type": "meme",
    },
    {
        "canonical": "好活",
        "members": ["好活", "好活当赏"],
        "relation": "expanded_variant",
        "reason": "基础表达和固定扩展句。",
        "content_type": "meme",
    },
    {
        "canonical": "币给你了",
        "members": ["你币有了", "币给你了"],
        "relation": "synonym_variant",
        "reason": "同一投币认可动作的语序变体。",
        "content_type": "catchphrase",
    },
    {
        "canonical": "抽象",
        "members": ["抽象", "太抽象了"],
        "relation": "grammatical_variant",
        "reason": "程度副词变体。",
        "content_type": "catchphrase",
    },
    {
        "canonical": "咕嘎",
        "members": ["咕嘎", "咕咕嘎嘎"],
        "relation": "reduplication_variant",
        "reason": "同一拟声口癖的叠词变体。",
        "content_type": "catchphrase",
    },
    {
        "canonical": "这下听懂了",
        "members": ["这下看懂了", "这下听懂了"],
        "relation": "template_variant",
        "reason": "“这下 + 感官动词 + 懂了”固定句式模板。",
        "content_type": "catchphrase",
    },
    {
        "canonical": "爷们儿",
        "members": ["爷们！", "爷们儿！"],
        "relation": "erhua_variant",
        "reason": "儿化和标点变体，是否为具体来源口癖仍待确认。",
        "content_type": "catchphrase",
    },
    {
        "canonical": "泪目",
        "members": ["泪目", "泪目了"],
        "relation": "grammatical_variant",
        "reason": "完成体助词变体。",
        "content_type": "catchphrase",
    },
]


CATCHPHRASE_SINGLETONS = {
    "man",
    "神了",
    "爆了",
    "还真是",
    "陌生",
    "羞死了",
    "这倒提醒我了",
    "神人",
    "确实",
    "无敌",
    "吓人",
    "难说",
    "离谱",
    "好家伙",
    "睡着了？",
    "保留节目",
    "天才！",
    "时间差不多喽",
    "有点意思",
    "没忍住",
    "笑麻了",
    "猫宁",
    "omg~",
    "woc",
    "ez",
    "sb",
    "nb",
    "早干嘛去了",
    "大家都早点休息吧",
    "运啊",
    "开了？",
    "绝杀",
    "图一乐",
}


RECONSIDER_AS_CATCHPHRASE = {
    "吓我一跳",
    "收到",
    "来了来了",
    "可爱捏",
    "我不敢看了",
    "害怕",
    "生气了",
    "也行",
    "我服了",
    "牛的",
    "我不行了",
    "舒服了",
    "狗运",
    "笑了",
    "给你！",
    "好耶",
    "合理",
    "哦对了",
    "笑死",
    "我靠",
    "好早",
    "太好了",
    "来咯",
    "苦也",
    "哇哦",
    "打卡",
    "好可爱",
    "来早了",
    "我的天",
    "专业对口",
    "好好笑",
    "救命",
    "经典",
    "言出法随",
    "闭环了",
    "啊这",
    "来劲了",
    "太牛了",
    "吃美了",
    "厉害",
    "大彻大悟",
    "要素察觉",
    "恭喜接广",
    "看力竭了",
    "神队",
}


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根节点必须是对象：{path}")
    return value


def _id(canonical: str) -> str:
    return "group-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _proposal(
    *,
    canonical: str,
    members: list[str],
    relation: str,
    reason: str,
    content_type: str,
    phrase_candidates: dict[str, dict[str, Any]],
    phrase_decisions: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    present = [phrase for phrase in members if phrase in phrase_decisions]
    if not present:
        return None
    candidate_rows = [phrase_candidates[phrase] for phrase in present if phrase in phrase_candidates]
    original_decisions = Counter(phrase_decisions[phrase]["decision"] for phrase in present)
    conflict = len(original_decisions) > 1
    if original_decisions.get("understand"):
        serving = "understand"
    elif original_decisions.get("refine"):
        serving = "refine"
    else:
        serving = "pending"
    circles: Counter[str] = Counter()
    rooms: Counter[str] = Counter()
    for candidate in candidate_rows:
        circles.update({item["name"]: int(item["count"]) for item in candidate.get("top_circles") or []})
        rooms.update({item["name"]: int(item["count"]) for item in candidate.get("live_rooms") or []})
    return {
        "group_id": _id(canonical),
        "canonical_expression": canonical,
        "members": present,
        "merge_relation": relation,
        "merge_reason": reason,
        "merge_status": "proposed" if len(present) > 1 else "singleton",
        "content_type_proposal": content_type,
        "serving_policy_proposal": serving,
        "original_decisions": dict(original_decisions),
        "decision_conflict": conflict,
        "message_count": sum(int(item.get("message_count") or 0) for item in candidate_rows),
        "distinct_content_count": sum(int(item.get("distinct_content_count") or 0) for item in candidate_rows),
        "day_count": max((int(item.get("day_count") or 0) for item in candidate_rows), default=0),
        "first_seen_date": min((item["first_seen_date"] for item in candidate_rows if item.get("first_seen_date")), default=None),
        "last_seen_date": max((item["last_seen_date"] for item in candidate_rows if item.get("last_seen_date")), default=None),
        "live_message_count": sum(int(item.get("live_message_count") or 0) for item in candidate_rows),
        "top_circles": [{"name": key, "count": value} for key, value in circles.most_common(3)],
        "live_rooms": [{"name": key, "count": value} for key, value in rooms.most_common(3)],
        "lifecycle": {
            "age_class_proposal": "current_observed",
            "last_observed_date": max(
                (item["last_seen_date"] for item in candidate_rows if item.get("last_seen_date")),
                default=None,
            ),
            "auto_publish": False,
        },
    }


def build_document(decisions: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    candidates = {item["phrase"]: item for item in summary.get("candidates") or []}
    phrase_decisions = {item["phrase"]: item for item in decisions.get("decisions") or []}
    retained = {
        phrase for phrase, item in phrase_decisions.items() if item.get("decision") in {"refine", "understand"}
    }
    merge_reconsider = {
        member
        for proposal in MERGE_PROPOSALS
        if proposal["content_type"] == "catchphrase"
        for member in proposal["members"]
        if phrase_decisions.get(member, {}).get("decision") == "reject"
    }
    reconsider = {
        phrase
        for phrase in RECONSIDER_AS_CATCHPHRASE
        if phrase_decisions.get(phrase, {}).get("decision") == "reject"
    } | merge_reconsider
    selected = retained | reconsider
    grouped: set[str] = set()
    groups: list[dict[str, Any]] = []
    for proposal in MERGE_PROPOSALS:
        row = _proposal(
            canonical=proposal["canonical"],
            members=proposal["members"],
            relation=proposal["relation"],
            reason=proposal["reason"],
            content_type=proposal["content_type"],
            phrase_candidates=candidates,
            phrase_decisions=phrase_decisions,
        )
        if row and any(member in selected for member in row["members"]):
            groups.append(row)
            grouped.update(row["members"])
    for phrase in sorted(selected - grouped):
        original = phrase_decisions[phrase]["decision"]
        content_type = "catchphrase" if phrase in CATCHPHRASE_SINGLETONS or phrase in reconsider else "meme"
        row = _proposal(
            canonical=phrase,
            members=[phrase],
            relation="singleton",
            reason=(
                "初审淘汰项中具有固定表达形态，建议按口癖/口头禅重新判断。"
                if phrase in reconsider
                else "初审保留项，等待二审确认内容类型。"
            ),
            content_type=content_type,
            phrase_candidates=candidates,
            phrase_decisions=phrase_decisions,
        )
        if row:
            row["serving_policy_proposal"] = "pending" if original == "reject" else row["serving_policy_proposal"]
            groups.append(row)
    groups.sort(
        key=lambda item: (
            0 if item["decision_conflict"] else 1,
            0 if item["merge_status"] == "proposed" else 1,
            0 if item["serving_policy_proposal"] == "refine" else 1,
            -item["message_count"],
            item["canonical_expression"],
        )
    )
    return {
        "schema_version": 1,
        "report_kind": "p0_meme_discovery_second_pass_review",
        "source_decisions": {
            "exported_at": decisions.get("exported_at"),
            "date_range": decisions.get("date_range"),
            "reviewed_count": len(decisions.get("decisions") or []),
        },
        "taxonomy": {
            "content_types": {
                "meme": "字面意义不能充分解释真实交流含义，并稳定依赖事件、名场面、谐音、同义替换或特殊句式模板。",
                "catchphrase": "真实含义通常仍接近字面，但因高频复用而承担稳定的态度或互动功能；可以是主播、圈层或更广泛网民的口头禅。",
                "ordinary": "字面即可完整解释，且没有稳定来源或群体风格锚点。",
                "noise": "平台界面、活动指令、广告、纯刷屏或采集伪影。",
            },
            "serving_policies": {
                "refine": "进入语义提炼；人工通过后才可能允许千叶主动使用。",
                "understand": "千叶需要识别并理解，但默认不得主动使用。",
                "skip": "不进入运行时梗库。",
            },
            "orthogonal_axes": True,
        },
        "summary": {
            "initial_reviewed_count": len(decisions.get("decisions") or []),
            "initial_refine_count": sum(item.get("decision") == "refine" for item in decisions.get("decisions") or []),
            "initial_understand_count": sum(
                item.get("decision") == "understand" for item in decisions.get("decisions") or []
            ),
            "initial_reject_count": sum(item.get("decision") == "reject" for item in decisions.get("decisions") or []),
            "retained_expression_count": len(retained),
            "catchphrase_reconsideration_count": len(reconsider),
            "review_group_count": len(groups),
            "merge_proposal_count": sum(item["merge_status"] == "proposed" for item in groups),
            "decision_conflict_count": sum(item["decision_conflict"] for item in groups),
        },
        "groups": groups,
    }


def _json_script(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def render_html(document: dict[str, Any]) -> str:
    data = _json_script(document)
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>千叶热梗二审：分类与合并</title>
<style>
:root{{--bg:#f4f4f1;--panel:#fff;--ink:#20221f;--muted:#6c716a;--line:#dcded7;--brand:#2459d3;--meme:#145b97;--catch:#7b4c12;--warn:#9a3329}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:13px/1.42 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}.page{{max-width:1480px;margin:auto;padding:18px 22px 40px}}
header{{display:flex;align-items:flex-end;justify-content:space-between;gap:16px}}h1{{margin:0;font-size:23px}}.sub,.muted{{color:var(--muted)}}button,select,input,.btn{{font:inherit}}button,.btn{{border:1px solid #cbd0c8;background:#fff;padding:6px 9px;border-radius:6px;cursor:pointer}}button.primary{{background:var(--brand);color:#fff;border-color:var(--brand)}}.btn input{{display:none}}
.definitions{{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin:12px 0}}.definition,.metric{{background:#fff;border:1px solid var(--line);border-radius:7px;padding:8px 10px}}.definition b{{display:block;margin-bottom:3px}}.definition span{{font-size:11px;color:var(--muted)}}.metrics{{display:grid;grid-template-columns:repeat(7,1fr);gap:7px;margin-bottom:10px}}.metric b{{font-size:18px;display:block}}.metric span{{font-size:11px;color:var(--muted)}}
.notice{{padding:8px 11px;border:1px solid #e2c475;background:#fff7df;border-radius:7px;margin:8px 0}}.toolbar{{position:sticky;top:0;z-index:3;background:rgba(244,244,241,.96);padding:8px 0;border-block:1px solid var(--line);display:flex;gap:6px;align-items:center;flex-wrap:wrap}}.toolbar input{{min-width:230px;flex:1;padding:7px 9px;border:1px solid #cbd0c8;border-radius:6px}}.filters button.active{{background:#26303f;color:#fff;border-color:#26303f}}.progress{{margin-left:auto;color:var(--muted)}}
main{{display:grid;gap:6px;margin-top:7px}}article{{display:grid;grid-template-columns:minmax(230px,1.2fr) minmax(330px,2fr) auto;gap:10px;align-items:center;background:#fff;border:1px solid var(--line);border-left:4px solid #aaa;border-radius:7px;padding:8px 10px}}article.meme{{border-left-color:var(--meme)}}article.catchphrase{{border-left-color:var(--catch)}}article.conflict{{box-shadow:inset 0 0 0 1px #e9a29b}}.canonical{{font-size:16px;font-weight:700}}.aliases{{margin-top:3px;color:var(--muted);font-size:11px}}.badges{{display:flex;gap:4px;flex-wrap:wrap;margin-top:4px}}.badge{{font-size:10px;padding:1px 6px;border-radius:999px;background:#eee}}.badge.meme{{background:#e3effa;color:#145b97}}.badge.catchphrase{{background:#f6ead8;color:#71460f}}.badge.conflict{{background:#ffe7e2;color:#92382d}}.badge.merge{{background:#eee4ff;color:#684092}}.facts{{display:flex;gap:10px;flex-wrap:wrap}}.context{{font-size:11px;color:var(--muted);margin-top:3px}}.actions{{display:grid;grid-template-columns:auto auto;gap:5px}}.actions select{{border:1px solid #cbd0c8;border-radius:5px;padding:5px}}.merge-actions{{grid-column:1/-1;display:flex;gap:4px}}.merge-actions button{{font-size:11px;padding:4px 7px}}.merge-actions button.active{{background:#26303f;color:#fff}}.empty{{padding:35px;text-align:center;color:var(--muted)}}
@media(max-width:1050px){{.definitions{{grid-template-columns:repeat(2,1fr)}}.metrics{{grid-template-columns:repeat(4,1fr)}}article{{grid-template-columns:1fr}}}}@media(max-width:600px){{.page{{padding:12px}}.definitions,.metrics{{grid-template-columns:repeat(2,1fr)}}header{{display:block}}}}
</style></head><body><div class="page"><header><div><h1>千叶热梗二审：分类、使用策略与合并</h1><div class="sub">单文件离线版 · 不展示原始弹幕 · 所有合并和分类均为待人工确认</div></div><div><label class="btn">导入二审 JSON<input id="import" type="file" accept=".json,application/json"></label> <button class="primary" id="export">导出二审 JSON</button></div></header>
<div class="definitions"><div class="definition"><b>梗</b><span>字面不足以解释真实含义；依赖事件、名场面、谐音、同义替换或句式模板。</span></div><div class="definition"><b>口癖 / 口头禅</b><span>含义通常接近字面，但因主播或圈层高频复用成为风格标记。</span></div><div class="definition"><b>仅理解</b><span>是使用策略，不是内容类型；梗和口癖都可以设为仅理解。</span></div><div class="definition"><b>合并</b><span>只合并同一表达的别称、谐音、同义变体或明确模板成员，不按“情绪相似”合并。</span></div></div>
<div class="notice">初审中的“进入提炼”不等于已经确认是梗；“淘汰”项也可能在这里作为口癖候选重新出现。原决定冲突的合并组默认采用更保守的“仅理解”。</div><section class="metrics" id="metrics"></section>
<div class="toolbar"><input id="search" type="search" placeholder="搜索规范表达、别称或圈层…"><div class="filters" id="filters"></div><span class="progress" id="progress"></span></div><main id="groups"></main></div>
<script id="data" type="application/json">{data}</script><script>
const REPORT=JSON.parse(document.getElementById('data').textContent),KEY='chiba-meme-second-pass:'+REPORT.source_decisions.date_range.start;let state={{filter:'all',q:''}},storage=true;
function read(){{try{{return JSON.parse(localStorage.getItem(KEY)||'{{}}')}}catch(e){{storage=false;return {{}}}}}}let decisions=read();
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));const fmt=n=>Number(n||0).toLocaleString('zh-CN');
function save(){{try{{localStorage.setItem(KEY,JSON.stringify(decisions))}}catch(e){{storage=false}}}}
const typeLabel={{meme:'梗',catchphrase:'口癖/口头禅',ordinary:'普通表达',noise:'噪音'}},serveLabel={{refine:'进入提炼',understand:'仅理解',skip:'跳过',pending:'待决定'}};
function effective(g,k){{return decisions[g.group_id]?.[k]||g[k+'_proposal']}}
function renderMetrics(){{const s=REPORT.summary,items=[['初审完成',s.initial_reviewed_count],['初审保留表达',s.retained_expression_count],['口癖重审',s.catchphrase_reconsideration_count],['二审条目',s.review_group_count],['建议合并',s.merge_proposal_count],['原决定冲突',s.decision_conflict_count],['未审',REPORT.groups.filter(g=>!decisions[g.group_id]).length]];document.getElementById('metrics').innerHTML=items.map(x=>`<div class="metric"><b>${{fmt(x[1])}}</b><span>${{x[0]}}</span></div>`).join('')}}
const defs=[['all','全部'],['conflict','决定冲突'],['merge','建议合并'],['meme','梗'],['catchphrase','口癖'],['pending','待决定'],['understand','仅理解']];
function matches(g){{const t=effective(g,'content_type'),s=effective(g,'serving_policy');if(state.filter==='conflict'&&!g.decision_conflict)return false;if(state.filter==='merge'&&g.merge_status!=='proposed')return false;if(['meme','catchphrase'].includes(state.filter)&&t!==state.filter)return false;if(['pending','understand'].includes(state.filter)&&s!==state.filter)return false;if(state.q&&!([g.canonical_expression,...g.members,...g.top_circles.map(x=>x.name)].join(' ').toLowerCase().includes(state.q)))return false;return true}}
function render(){{const rows=REPORT.groups.filter(matches),root=document.getElementById('groups');root.innerHTML=rows.length?rows.map(g=>{{const d=decisions[g.group_id]||{{}},t=effective(g,'content_type'),s=effective(g,'serving_policy'),m=d.merge_decision||('proposed'===g.merge_status?'pending':'not_applicable');return `<article class="${{t}} ${{g.decision_conflict?'conflict':''}}" data-id="${{g.group_id}}"><div><div class="canonical">${{esc(g.canonical_expression)}}</div><div class="aliases">成员：${{g.members.map(esc).join(' · ')}}</div><div class="badges"><span class="badge ${{t}}">${{typeLabel[t]}}</span>${{g.merge_status==='proposed'?'<span class="badge merge">建议合并</span>':''}}${{g.decision_conflict?'<span class="badge conflict">原决定冲突</span>':''}}</div></div><div><div class="facts"><span><b>${{fmt(g.message_count)}}</b> 次</span><span><b>${{fmt(g.distinct_content_count)}}</b> 内容/场次</span><span>${{g.day_count}} 天</span><span>${{g.first_seen_date||'-'}}～${{g.last_seen_date||'-'}}</span></div><div class="context">${{esc(g.merge_reason)}} · 圈层：${{esc(g.top_circles.map(x=>x.name).join(' / ')||'未分类')}}${{g.live_rooms.length?' · 直播：'+esc(g.live_rooms.map(x=>x.name).join(' / ')):''}}</div></div><div class="actions"><select data-key="content_type"><option value="meme" ${{t==='meme'?'selected':''}}>梗</option><option value="catchphrase" ${{t==='catchphrase'?'selected':''}}>口癖/口头禅</option><option value="ordinary" ${{t==='ordinary'?'selected':''}}>普通表达</option><option value="noise" ${{t==='noise'?'selected':''}}>噪音</option></select><select data-key="serving_policy"><option value="pending" ${{s==='pending'?'selected':''}}>待决定</option><option value="refine" ${{s==='refine'?'selected':''}}>进入提炼</option><option value="understand" ${{s==='understand'?'selected':''}}>仅理解</option><option value="skip" ${{s==='skip'?'selected':''}}>跳过</option></select>${{g.merge_status==='proposed'?`<div class="merge-actions"><button data-merge="accept" class="${{m==='accept'?'active':''}}">确认合并</button><button data-merge="split" class="${{m==='split'?'active':''}}">不要合并</button></div>`:''}}</div></article>`}}).join(''):'<div class="empty">当前筛选没有条目。</div>';root.querySelectorAll('select').forEach(el=>el.onchange=()=>{{const id=el.closest('article').dataset.id;decisions[id]={{...(decisions[id]||{{}}),[el.dataset.key]:el.value,updated_at:new Date().toISOString()}};save();renderMetrics();render()}});root.querySelectorAll('[data-merge]').forEach(el=>el.onclick=()=>{{const id=el.closest('article').dataset.id;decisions[id]={{...(decisions[id]||{{}}),merge_decision:el.dataset.merge,updated_at:new Date().toISOString()}};save();renderMetrics();render()}});document.getElementById('progress').textContent=`显示 ${{rows.length}} / ${{REPORT.groups.length}} · 已改 ${{Object.keys(decisions).length}}`;document.querySelectorAll('#filters button').forEach(b=>b.classList.toggle('active',b.dataset.f===state.filter))}}
function exportData(){{const payload={{schema_version:1,report_kind:'p0_meme_discovery_second_pass_decisions',source_decisions:REPORT.source_decisions,exported_at:new Date().toISOString(),decisions:Object.entries(decisions).map(([group_id,value])=>({{group_id,canonical_expression:REPORT.groups.find(g=>g.group_id===group_id)?.canonical_expression||'',...value}}))}};const blob=new Blob([JSON.stringify(payload,null,2)+'\\n'],{{type:'application/json'}}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='meme-second-pass-decisions.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)}}
async function importData(file){{if(!file)return;try{{const p=JSON.parse(await file.text());if(p.report_kind!=='p0_meme_discovery_second_pass_decisions'||!Array.isArray(p.decisions))throw Error('不是本页导出的二审 JSON');for(const x of p.decisions)if(x.group_id)decisions[x.group_id]=x;save();renderMetrics();render();alert(`已导入 ${{p.decisions.length}} 条二审决定`)}}catch(e){{alert('导入失败：'+e.message)}}}}
document.getElementById('filters').innerHTML=defs.map(x=>`<button data-f="${{x[0]}}">${{x[1]}}</button>`).join('');document.querySelectorAll('#filters button').forEach(b=>b.onclick=()=>{{state.filter=b.dataset.f;render()}});document.getElementById('search').oninput=e=>{{state.q=e.target.value.trim().toLowerCase();render()}};document.getElementById('export').onclick=exportData;document.getElementById('import').onchange=async e=>{{await importData(e.target.files?.[0]);e.target.value=''}};renderMetrics();render();
</script></body></html>"""


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    args = _args()
    decisions = _read(args.decisions.expanduser().resolve())
    summary = _read(args.summary.expanduser().resolve())
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else args.summary.expanduser().resolve().parent / "second-pass"
    )
    document = build_document(decisions, summary)
    json_path = output_dir / "second-pass-review.json"
    html_path = output_dir / "second-pass-review.html"
    _write_json(json_path, document)
    html_path.write_text(render_html(document), encoding="utf-8")
    print(json.dumps({"review_page": str(html_path), "review_json": str(json_path), "summary": document["summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
