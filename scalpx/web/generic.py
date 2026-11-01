"""通用实体存储：schema 驱动的增删改查。

安全边界
--------
表名、列名全部来自 `schema.ENTITIES`（代码内注册），**绝不拼接用户输入**；
所有值走参数化查询。前端只能提交"字段值"，不能影响 SQL 结构。

错误语义
--------
所有失败都以 `EntityError` 抛出，`code` 与 HTTP 状态由 api 层映射：
    VALIDATION_FAILED(400) / DUPLICATE(409) / NOT_FOUND(404) / BAD_REQUEST(400)
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from .repo import WebRepo
from .schema import EntitySpec, Issue


class EntityError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, issues: list[Issue] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.issues = issues or []


class EntityStore:
    """按 EntitySpec 操作真实表。"""

    def __init__(self, web: WebRepo) -> None:
        self.web = web

    # ------------------------------------------------------------------ #
    # 行编解码
    # ------------------------------------------------------------------ #
    def _decode(self, spec: EntitySpec, row: sqlite3.Row | dict) -> dict:
        item = dict(row)
        for f in spec.fields:
            if f.name not in item:
                continue
            if f.type == "bool":
                item[f.name] = bool(item[f.name])
            elif f.type == "json":
                raw = item[f.name]
                if isinstance(raw, str):
                    try:
                        item[f.name] = json.loads(raw) if raw.strip() else {}
                    except json.JSONDecodeError:
                        item[f.name] = {}
        return item

    def _encode(self, spec: EntitySpec, data: dict) -> dict:
        out: dict[str, Any] = {}
        for f in spec.fields:
            if f.name not in data:
                continue
            value = data[f.name]
            if f.type == "bool":
                out[f.name] = 1 if value else 0
            elif f.type == "json":
                out[f.name] = json.dumps(value, ensure_ascii=False)
            else:
                out[f.name] = value
        return out

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def list(self, spec: EntitySpec, *, q: str = "", limit: int = 200) -> list[dict]:
        sql = f"SELECT * FROM {spec.table} WHERE 1=1"
        params: list[Any] = []
        if q and spec.search_fields:
            clause = " OR ".join(f"{f} LIKE ?" for f in spec.search_fields)
            sql += f" AND ({clause})"
            params.extend([f"%{q}%"] * len(spec.search_fields))
        sql += f" ORDER BY {spec.order_by} LIMIT ?"
        params.append(limit)
        rows = self.web.connection.execute(sql, params).fetchall()
        return [self._decode(spec, r) for r in rows]

    def get(self, spec: EntitySpec, rid: int) -> dict:
        row = self.web.connection.execute(
            f"SELECT * FROM {spec.table} WHERE id = ?", (rid,)
        ).fetchone()
        if row is None:
            raise EntityError("NOT_FOUND", f"{spec.label}不存在（id={rid}）", 404)
        return self._decode(spec, row)

    def _check_unique(self, spec: EntitySpec, data: dict, *, exclude_id: int | None = None) -> None:
        for keys in spec.unique:
            if not all(k in data for k in keys):
                continue
            where = " AND ".join(f"{k} = ?" for k in keys)
            params: list[Any] = [data[k] for k in keys]
            sql = f"SELECT id FROM {spec.table} WHERE {where}"
            if exclude_id is not None:
                sql += " AND id <> ?"
                params.append(exclude_id)
            row = self.web.connection.execute(sql, params).fetchone()
            if row is not None:
                label = "/".join(keys)
                raise EntityError(
                    "DUPLICATE",
                    f"{label} 已存在：{data[keys[0]]}",
                    409,
                    issues=[Issue(keys[0], f"{label} 重复")],
                )

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def create(self, spec: EntitySpec, payload: dict) -> dict:
        data, issues = spec.normalize(payload, partial=False)
        if issues:
            raise EntityError("VALIDATION_FAILED", "；".join(f"{i.field}: {i.message}" for i in issues), 400, issues)

        self._check_unique(spec, data)
        encoded = self._encode(spec, data)
        now = int(time.time())
        encoded["created_ts"] = now
        encoded["updated_ts"] = now

        columns = ", ".join(encoded.keys())
        marks = ", ".join("?" * len(encoded))
        with self.web.lock:
            try:
                cur = self.web.connection.execute(
                    f"INSERT INTO {spec.table} ({columns}) VALUES ({marks})",
                    list(encoded.values()),
                )
                self.web.connection.commit()
            except sqlite3.IntegrityError as exc:
                raise EntityError("DUPLICATE", f"违反唯一约束：{exc}", 409) from exc
            rid = int(cur.lastrowid or 0)
        return self.get(spec, rid)

    def update(self, spec: EntitySpec, rid: int, payload: dict) -> dict:
        current = self.get(spec, rid)
        # 只合并 schema 内字段：当前行里的 id/created_ts 等不属于 schema，
        # 混进校验会被判为"未知字段"。
        merged = {name: current[name] for name in spec.field_names if name in current}
        merged.update(payload)
        data, issues = spec.normalize(merged, partial=False)
        if issues:
            raise EntityError("VALIDATION_FAILED", "；".join(f"{i.field}: {i.message}" for i in issues), 400, issues)

        self._check_unique(spec, data, exclude_id=rid)
        encoded = self._encode(spec, data)
        encoded["updated_ts"] = int(time.time())
        assignments = ", ".join(f"{k} = ?" for k in encoded)
        with self.web.lock:
            try:
                self.web.connection.execute(
                    f"UPDATE {spec.table} SET {assignments} WHERE id = ?",
                    [*encoded.values(), rid],
                )
                self.web.connection.commit()
            except sqlite3.IntegrityError as exc:
                raise EntityError("DUPLICATE", f"违反唯一约束：{exc}", 409) from exc
        return self.get(spec, rid)

    def delete(self, spec: EntitySpec, rid: int) -> dict:
        row = self.get(spec, rid)
        with self.web.lock:
            self.web.connection.execute(f"DELETE FROM {spec.table} WHERE id = ?", (rid,))
            self.web.connection.commit()
        return row

    def reorder(self, spec: EntitySpec, ids: list[int]) -> int:
        if not spec.reorderable:
            raise EntityError("BAD_REQUEST", f"{spec.label}不支持排序", 400)
        with self.web.lock:
            for index, rid in enumerate(ids):
                self.web.connection.execute(
                    f"UPDATE {spec.table} SET sort_order = ?, updated_ts = ? WHERE id = ?",
                    ((index + 1) * 10, int(time.time()), rid),
                )
            self.web.connection.commit()
        return len(ids)

    # ------------------------------------------------------------------ #
    # 校验（不写库，供前端实时提示与 AI 纠错使用）
    # ------------------------------------------------------------------ #
    def validate(self, spec: EntitySpec, payload: dict, *, partial: bool = False, rid: int | None = None) -> dict:
        data, issues = spec.normalize(payload, partial=partial)
        duplicate = None
        if not issues:
            try:
                self._check_unique(spec, data, exclude_id=rid)
            except EntityError as exc:
                duplicate = {"code": exc.code, "message": exc.message}
        return {
            "valid": not issues and duplicate is None,
            "normalized": data,
            "issues": [i.as_dict() for i in issues],
            "duplicate": duplicate,
        }
