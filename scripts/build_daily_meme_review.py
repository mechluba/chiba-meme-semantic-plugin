#!/usr/bin/env python3
"""每天汇总昨天的热梗结果，生成终审页面并可选通知飞书机器人。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from meme_discovery.daily_review import (  # noqa: E402
    LOCAL_TZ,
    WEBHOOK_ENV,
    DailyReviewError,
    build_daily_review,
    notify_feishu,
    record_notification,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, help="汇总日期，默认 Asia/Shanghai 的昨天")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=REPO_ROOT / "out" / "p0-meme-discovery",
        help="热梗发现归档根目录",
    )
    parser.add_argument("--output-dir", type=Path, help="覆盖本次汇总输出目录")
    parser.add_argument(
        "--require-notify",
        action="store_true",
        help=f"要求通过 {WEBHOOK_ENV} 成功通知飞书，否则以失败退出",
    )
    args = parser.parse_args()
    target_date = args.date or (datetime.now(LOCAL_TZ).date() - timedelta(days=1))
    output_root = args.output_root.expanduser().resolve()
    try:
        result = build_daily_review(
            target_date=target_date,
            output_root=output_root,
            output_dir=args.output_dir,
        )
        webhook = os.environ.get(WEBHOOK_ENV, "").strip()
        if webhook:
            notification = notify_feishu(result, webhook=webhook)
            record_notification(output_root, result, notification)
        elif args.require_notify:
            raise DailyReviewError(f"没有设置 {WEBHOOK_ENV}")
    except (DailyReviewError, OSError, ValueError) as exc:
        print(f"每日热梗审核汇总失败：{exc}", file=sys.stderr)
        return 4
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
