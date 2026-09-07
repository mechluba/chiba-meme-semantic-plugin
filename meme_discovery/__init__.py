"""离线热梗候选发现流水线。

这里的输出只用于人工审核，不会修改或发布运行时梗包。
"""

from .pipeline import run_discovery

__all__ = ["run_discovery"]
