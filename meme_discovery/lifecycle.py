"""梗库生命周期降权初筛与淘汰候选冷却复审。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import hashlib
import random


def parse_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def time_decay_weight(
    last_observed_at: Any,
    *,
    now: datetime,
    half_life_days: float,
    floor: float = 0.05,
) -> float:
    """只提供待审建议权重，不直接改运行时梗库。"""
    observed = parse_timestamp(last_observed_at)
    if observed is None:
        return floor
    age_days = max(0.0, (now.astimezone(timezone.utc) - observed).total_seconds() / 86_400)
    weight = 2 ** (-age_days / max(half_life_days, 1.0))
    return round(max(floor, min(1.0, weight)), 4)


def select_inventory_decay_review(
    cards: list[dict[str, Any]],
    observations: dict[str, dict[str, Any]],
    config: dict[str, Any],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    """选择需要人工判断“保持/降权/退役”的库存卡，不自动修改卡片。"""
    default_half_life = float(config.get("default_half_life_days", 45))
    half_life_by_class = config.get("half_life_days_by_age_class") or {}
    threshold = float(config.get("review_below_weight", 0.45))
    min_age_days = float(config.get("minimum_age_days", 21))
    result: list[dict[str, Any]] = []
    for card in cards:
        card_id = str(card.get("card_id") or "")
        observation = observations.get(card_id) or {}
        last_seen = observation.get("last_observed_at")
        parsed = parse_timestamp(last_seen)
        age_days = (
            round(max(0.0, (now.astimezone(timezone.utc) - parsed).total_seconds() / 86_400), 2)
            if parsed
            else None
        )
        age_class = str(card.get("age_class") or "current_observed")
        half_life = float(half_life_by_class.get(age_class, default_half_life))
        proposed_weight = time_decay_weight(last_seen, now=now, half_life_days=half_life)
        if proposed_weight >= threshold or (age_days is not None and age_days < min_age_days):
            continue
        result.append(
            {
                "card_id": card_id,
                "canonical_expression": card.get("canonical_expression"),
                "age_class": age_class,
                "last_observed_at": last_seen,
                "age_days": age_days,
                "observed_30d_count": int(observation.get("observed_30d_count") or 0),
                "current_weight": float((card.get("lifecycle") or {}).get("weight") or 1.0),
                "proposed_weight": proposed_weight,
                "review_actions": ["KEEP", "DOWNRANK", "RETIRE"],
                "auto_apply": False,
            }
        )
    result.sort(key=lambda item: (item["proposed_weight"], item["observed_30d_count"], str(item["card_id"])))
    return result


def select_rejected_for_rereview(
    rejected: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    now: datetime,
    seed: str | None = None,
) -> list[dict[str, Any]]:
    """从已过冷却期的淘汰项中稳定随机抽样，避免连续重复审同一项。"""
    cooldown_days = max(1, int(config.get("cooldown_days", 30)))
    sample_size = max(0, int(config.get("sample_size", 20)))
    eligible: list[dict[str, Any]] = []
    for item in rejected:
        last_reviewed = parse_timestamp(item.get("last_rereviewed_at") or item.get("rejected_at"))
        if last_reviewed is None:
            last_reviewed = datetime(1970, 1, 1, tzinfo=timezone.utc)
        elapsed_days = (now.astimezone(timezone.utc) - last_reviewed).total_seconds() / 86_400
        if elapsed_days < cooldown_days:
            continue
        eligible.append(
            {
                **item,
                "cooldown_days": cooldown_days,
                "days_since_last_review": round(elapsed_days, 2),
                "review_actions": ["KEEP_REJECTED", "RESTORE_AS_MEME", "RESTORE_AS_CATCHPHRASE"],
                "auto_apply": False,
            }
        )
    seed_value = seed or now.astimezone(timezone.utc).date().isoformat()
    numeric_seed = int(hashlib.sha256(seed_value.encode("utf-8")).hexdigest()[:16], 16)
    random.Random(numeric_seed).shuffle(eligible)
    return eligible[:sample_size]
