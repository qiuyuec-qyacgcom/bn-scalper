"""scalpx —— 面向币安 U 本位永续的超短线交易系统。

分层（自上而下依赖，反向不依赖）：

    models      纯数据结构，无依赖（含订单流/盘口快照）
    config      配置加载/校验
    indicators  增量指标（回测/实盘共用）
    microstructure 成交流(CVD/吸收度) + 盘口(OBI/微价格)，回测实盘同口径
    data        行情接入 + 存储 + REST 客户端 + 订单流数据源
    portfolio   账务
    strategy    策略（只出意图）
    risk        风控（唯一有权拒绝/定仓位的模块）
    execution   订单执行（Broker 抽象 + 模拟撮合 + OMS）
    engine      回测引擎 / 实盘引擎（唯一知道"怎么跑"的地方）
    metrics     绩效
    reporting   报表
    cli         命令行

关键约束：strategy 不得 import risk/execution/engine；risk 不得 import execution；
execution 不得 import engine。任何一个方向违规都会被 tests/test_architecture.py 拦住。
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]
