"""`Preference_Store` 服务层与 `/api/preferences` 端点测试（任务 11.1，R18）。

守的是 tasks.md 11.1 的可验收点，逐条对应：

- **创建默认未启用**（R18.4）：`create_rule` 恒 `enabled=False`；服务层没有 `enabled` 参数。
- **显式启用是独立动作**：`set_enabled(..., True)` 是唯一能置 `enabled=True` 的路径；`update_rule`
  改不了 `enabled`；创建/PATCH 的请求体里没有 `enabled` 字段（API 层 422 拒绝 `enabled=true`）。
- **20 条上限**（R18.11）：第 21 条启用抛 `PreferenceRuleLimitError` /
  返回 `PREFERENCE_RULE_LIMIT_REACHED`。
- **证据不足**（R18.10）：`source_decision_ids < 2` → `low_evidence=True`。
- **越界拒绝**（R18.8）：指向硬约束开关或非软目标 component →
  `PreferenceRuleOutOfScopeError` / `PREFERENCE_RULE_OUT_OF_SCOPE`。
  （结构性证明另见 `tests/structure/test_preference_rule_scope.py`。）
- **CRUD 全部写审计**（R18.12）：CREATE/UPDATE/ENABLE/DISABLE/DELETE 各产生一条
  `PREFERENCE_RULE_CHANGE` 记录。
- **停用后被忽略**（R18.9）：停用的规则不进 `enabled_only` 列表（快照只加载 enabled，已有测试覆盖
  `test_baseline_fcfs`/`snapshot_loader`；这里断言列表口径）。

走真实 SQLite 文件与真实 `create_app` 装配（审计引擎因此指向同库，审计断言直接读它），不 mock。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.audit import set_audit_engine
from app.db.models import AuditLog, Base
from app.main import create_app
from app.services import preferences as store
from app.services.preferences import (
    PreferenceRuleLimitError,
    PreferenceRuleNotFoundError,
    PreferenceRuleOutOfScopeError,
)
from app.settings import Settings

LOGIN = "/api/auth/login"
PREFS = "/api/preferences"

AVOID_ORDER = {
    "kind": "AVOID_MACHINE_FOR_ORDER",
    "order_id": "ORD-007",
    "machine_id": "CNC-03",
    "weight_delta": 2.0,
}


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture
def app_settings(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_file = tmp_path / "preferences.db"
    valid_env.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
def application(app_settings: Settings) -> Iterator[object]:
    app = create_app(app_settings)
    Base.metadata.create_all(app.state.engine)
    yield app
    set_audit_engine(None)


@pytest.fixture
def factory(application: object) -> sessionmaker[Session]:
    f: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    return f


@pytest.fixture
def client(application: object, app_settings: Settings) -> Iterator[TestClient]:
    with TestClient(application) as test_client:  # type: ignore[arg-type]
        test_client.post(
            LOGIN, json={"password": app_settings.session_shared_password.get_secret_value()}
        )
        yield test_client


def _audit_rows(application: object, event_type: str) -> list[AuditLog]:
    engine: Engine = application.state.engine  # type: ignore[attr-defined]
    with Session(engine) as s:
        return list(
            s.execute(
                select(AuditLog)
                .where(AuditLog.event_category == "PREFERENCE_RULE_CHANGE")
                .where(AuditLog.event_type == event_type)
            ).scalars()
        )


# --------------------------------------------------------------------------
# 服务层
# --------------------------------------------------------------------------


def test_create_rule_defaults_to_disabled(factory: sessionmaker[Session]) -> None:
    """创建即未启用（R18.4）：create_rule 没有 enabled 参数，落库 enabled=False。"""
    with factory() as db:
        view = store.create_rule(db, human_text="ORD-007 避开 CNC-03", structured_form=AVOID_ORDER)
        db.commit()
        assert view.enabled is False


def test_create_marks_low_evidence_when_sources_below_two(factory: sessionmaker[Session]) -> None:
    """source_decision_ids < 2 → low_evidence=True（R18.10）；≥2 → False。"""
    with factory() as db:
        one = store.create_rule(
            db, human_text="一条来源", structured_form=AVOID_ORDER, source_decision_ids=["DEC-1"]
        )
        two = store.create_rule(
            db,
            human_text="两条来源",
            structured_form=AVOID_ORDER,
            source_decision_ids=["DEC-1", "DEC-2"],
        )
        db.commit()
        assert one.low_evidence is True
        assert two.low_evidence is False
        assert two.source_decision_ids == ("DEC-1", "DEC-2")


def test_update_rule_cannot_change_enabled(factory: sessionmaker[Session]) -> None:
    """update_rule 改不了 enabled——它没有该形参，启用只能经 set_enabled。"""
    with factory() as db:
        view = store.create_rule(db, human_text="text", structured_form=AVOID_ORDER)
        db.commit()
        updated = store.update_rule(db, view.rule_id, human_text="edited text")
        db.commit()
        assert updated.enabled is False
        assert updated.human_text == "edited text"


def test_set_enabled_is_the_only_enable_path(factory: sessionmaker[Session]) -> None:
    """set_enabled(..., True) 启用，False 停用；停用后不在 enabled_only 列表里（R18.9）。"""
    with factory() as db:
        view = store.create_rule(db, human_text="text", structured_form=AVOID_ORDER)
        db.commit()
        store.set_enabled(db, view.rule_id, True)
        db.commit()
        assert store.get_rule(db, view.rule_id).enabled is True
        assert any(v.rule_id == view.rule_id for v in store.list_rules(db, enabled_only=True))

        store.set_enabled(db, view.rule_id, False)
        db.commit()
        assert store.get_rule(db, view.rule_id).enabled is False
        assert not any(
            v.rule_id == view.rule_id for v in store.list_rules(db, enabled_only=True)
        )


def test_enable_beyond_twenty_raises(factory: sessionmaker[Session]) -> None:
    """启用满 20 条后，第 21 条启用抛 PreferenceRuleLimitError（R18.11）。"""
    with factory() as db:
        ids = []
        for i in range(store.MAX_ENABLED_RULES + 1):
            v = store.create_rule(
                db,
                human_text=f"rule {i}",
                structured_form={
                    "kind": "AVOID_MACHINE_FOR_ORDER",
                    "order_id": f"ORD-{i:03d}",
                    "machine_id": "CNC-03",
                },
            )
            ids.append(v.rule_id)
        db.commit()
        for rid in ids[: store.MAX_ENABLED_RULES]:
            store.set_enabled(db, rid, True)
        db.commit()
        assert store.enabled_rule_count(db) == store.MAX_ENABLED_RULES
        with pytest.raises(PreferenceRuleLimitError):
            store.set_enabled(db, ids[store.MAX_ENABLED_RULES], True)


def test_out_of_scope_form_rejected(factory: sessionmaker[Session]) -> None:
    """指向硬约束开关的 component 被拒（R18.8、EVAL-206）。"""
    with factory() as db, pytest.raises(PreferenceRuleOutOfScopeError):
        store.create_rule(
            db,
            human_text="试图放宽硬约束",
            structured_form={
                "kind": "ADJUST_OBJECTIVE_WEIGHT",
                "component": "allow_shift_overflow",  # 非 6 个软目标之一
                "multiplier": 1.5,
            },
        )


def test_delete_rule_removes_it(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        view = store.create_rule(db, human_text="text", structured_form=AVOID_ORDER)
        db.commit()
        store.delete_rule(db, view.rule_id)
        db.commit()
        with pytest.raises(PreferenceRuleNotFoundError):
            store.get_rule(db, view.rule_id)


def test_each_crud_action_writes_audit(
    application: object, factory: sessionmaker[Session]
) -> None:
    """CREATE / UPDATE / ENABLE / DISABLE / DELETE 各写一条 PREFERENCE_RULE_CHANGE（R18.12）。"""
    with factory() as db:
        view = store.create_rule(db, human_text="text", structured_form=AVOID_ORDER)
        db.commit()
        store.update_rule(db, view.rule_id, human_text="edited")
        db.commit()
        store.set_enabled(db, view.rule_id, True)
        db.commit()
        store.set_enabled(db, view.rule_id, False)
        db.commit()
        store.delete_rule(db, view.rule_id)
        db.commit()

    for event_type in ("CREATE", "UPDATE", "ENABLE", "DISABLE", "DELETE"):
        rows = _audit_rows(application, event_type)
        assert len(rows) == 1, f"缺少 {event_type} 审计记录"
        assert rows[0].subject_id == view.rule_id


# --------------------------------------------------------------------------
# API 层
# --------------------------------------------------------------------------


def test_post_rejects_enabled_true(client: TestClient) -> None:
    """POST 请求体带 enabled=true 被 Pydantic 拒（extra=forbid）——创建不能静默启用。"""
    resp = client.post(
        PREFS, json={"human_text": "x", "structured_form": AVOID_ORDER, "enabled": True}
    )
    assert resp.status_code == 422, resp.text


def test_post_creates_disabled_rule(client: TestClient) -> None:
    resp = client.post(
        PREFS, json={"human_text": "ORD-007 避开 CNC-03", "structured_form": AVOID_ORDER}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["enabled"] is False
    assert body["kind"] == "AVOID_MACHINE_FOR_ORDER"


def test_patch_rejects_enabled_field(client: TestClient) -> None:
    """通用 PATCH 请求体带 enabled 被拒——启用只能经 /enable。"""
    created = client.post(PREFS, json={"human_text": "x", "structured_form": AVOID_ORDER}).json()
    resp = client.patch(f"{PREFS}/{created['rule_id']}", json={"enabled": True})
    assert resp.status_code == 422, resp.text


def test_enable_then_disable_via_dedicated_endpoints(client: TestClient) -> None:
    created = client.post(PREFS, json={"human_text": "x", "structured_form": AVOID_ORDER}).json()
    rid = created["rule_id"]
    assert client.post(f"{PREFS}/{rid}/enable").json()["enabled"] is True
    assert client.post(f"{PREFS}/{rid}/disable").json()["enabled"] is False


def test_out_of_scope_component_returns_error_code(client: TestClient) -> None:
    """越界 component 返回 PREFERENCE_RULE_OUT_OF_SCOPE 或被判别联合拒为 422（R18.8）。"""
    resp = client.post(
        PREFS,
        json={
            "human_text": "试图放宽硬约束",
            "structured_form": {
                "kind": "ADJUST_OBJECTIVE_WEIGHT",
                "component": "allow_shift_overflow",
                "multiplier": 1.5,
            },
        },
    )
    assert resp.status_code == 422, resp.text
    # 判别联合先拦（Pydantic 422 校验错误），或服务层显式码；两者都合规。
    code = resp.json().get("error", {}).get("code")
    assert code in {"PREFERENCE_RULE_OUT_OF_SCOPE", None} or "detail" in resp.json()


def test_enable_cap_returns_limit_reached(client: TestClient) -> None:
    """启用满 20 条后，第 21 条启用返回 409 PREFERENCE_RULE_LIMIT_REACHED（R18.11）。"""
    rule_ids = []
    for i in range(store.MAX_ENABLED_RULES + 1):
        body = client.post(
            PREFS,
            json={
                "human_text": f"rule {i}",
                "structured_form": {
                    "kind": "AVOID_MACHINE_FOR_ORDER",
                    "order_id": f"ORD-{i:03d}",
                    "machine_id": "CNC-03",
                },
            },
        ).json()
        rule_ids.append(body["rule_id"])
    for rid in rule_ids[: store.MAX_ENABLED_RULES]:
        assert client.post(f"{PREFS}/{rid}/enable").status_code == 200
    resp = client.post(f"{PREFS}/{rule_ids[store.MAX_ENABLED_RULES]}/enable")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "PREFERENCE_RULE_LIMIT_REACHED"


def test_list_reports_enabled_count_and_cap(client: TestClient) -> None:
    client.post(PREFS, json={"human_text": "x", "structured_form": AVOID_ORDER})
    body = client.get(PREFS).json()
    assert body["max_enabled"] == store.MAX_ENABLED_RULES
    assert body["enabled_count"] == 0
    assert body["total"] == 1


def test_unknown_rule_returns_not_found(client: TestClient) -> None:
    resp = client.get(f"{PREFS}/PR-does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PREFERENCE_RULE_NOT_FOUND"


def test_write_endpoints_require_auth(application: object) -> None:
    """未认证的写请求被 Session_Auth 拦下（R23.12）。"""
    with TestClient(application) as anon:  # type: ignore[arg-type]
        resp = anon.post(PREFS, json={"human_text": "x", "structured_form": AVOID_ORDER})
        assert resp.status_code == 401
