"""scalpx Web 应用：前后台一体化服务（仅依赖标准库）。

分层：
    server.py   HTTP 传输层（路由分发、静态资源、进程生命周期）
    api.py      接口层（参数校验、权限判定、响应封装）
    repo.py     数据访问层（web.db 用户/审计 + 交易库只读查询）
    auth.py     认证与会话（PBKDF2 口令、HMAC 令牌、登录限流）
    tomlio.py   配置读写（标准库只有 tomllib 读，这里补一个最小写入器）
    static/     前台(index.html) 与后台(admin.html) 单页应用

设计约束（与主项目一致）：
- 零第三方依赖：`http.server` + `sqlite3` + `hmac`，任何 Python 3.11+ 机器直接跑。
- 前后端只通过 `/api/*` 的 JSON 契约通信，静态页面不含任何服务端模板。
- 权限在接口层统一判定，页面只负责隐藏入口（真正的边界在服务端）。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
