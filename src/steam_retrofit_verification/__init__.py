"""蒸汽利用技改收益核验服务。

在 beverage_ops_foundation 基础能力（操作者、场所、审计链、可注入时钟）之上，
登记设备边界、计量点、校准有效期、批次能耗，并以冻结快照完成基准期/验证期
对比，区分缺失、异常、迟到读数，支持工程替代计算与独立复核确认。
"""

from .service import VerificationService

__all__ = ["VerificationService"]
