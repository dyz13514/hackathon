/**
 * 状态看板视图（design.md Components §6 `/` 行，任务 3.7）。
 *
 * 五个卡片区，逐条对应 §6 的 `/` 行与 R1：
 * 1. 五类实体（Order / Material / Machine / Worker / Plan）的当前状态（R1.1）；
 * 2. 每条显示 `source` 徽章与 `last_updated_at`（R1.3）；
 * 3. `Order.notes` 非空时渲染为纯文本 + `untrusted` 徽章，不解释任何指令语义（R1.4）；
 * 4. 后端不可用时显示 `DATA_UNAVAILABLE` 与上次成功加载时间（R1.5）。
 *
 * 数据来自 `GET /api/state/dashboard`（只读端点，无需认证）。首屏 3 秒内渲染（R1.2）：
 * 一次请求拿全部五类，渲染是纯映射，无额外往返。
 *
 * 可访问性（R27.9）：区块用 `<section aria-labelledby>`；状态信息（如 `untrusted`、
 * `DATA_UNAVAILABLE`、疑似注入）除样式外都带图标字符与文字，不只依赖颜色；刷新按钮
 * 可键盘到达并有 `aria-label`。JSX 默认转义文本，`notes` 因此不可能作为标记被解释。
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError } from '../api/client';
import { getDashboard, type Dashboard as DashboardData } from '../api/state';

/** 本地化 ISO 时间戳为可读文本；无值时给出占位。 */
function formatTimestamp(iso: string | null): string {
  if (!iso) {
    return '—';
  }
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleString();
}

/** `source` 徽章：文字即含义，不依赖颜色（R27.9）。 */
function SourceBadge({ source }: { source: string }) {
  return (
    <span className="badge badge-source" aria-label={`来源：${source}`}>
      {source}
    </span>
  );
}

export function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // 上次成功加载时刻（R1.5）：后端不可用时仍要能告诉规划员「你看到的是什么时候的状态」。
  const lastSuccessRef = useRef<Date | null>(null);
  const [lastSuccess, setLastSuccess] = useState<Date | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await getDashboard();
      setData(result);
      const now = new Date();
      lastSuccessRef.current = now;
      setLastSuccess(now);
    } catch (err) {
      const message =
        err instanceof ApiError
          ? `DATA_UNAVAILABLE（${err.code}）：${err.message}`
          : 'DATA_UNAVAILABLE：后端数据服务不可用。';
      setError(message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section aria-labelledby="dashboard-heading" className="dashboard">
      <div className="dashboard-header">
        <h2 id="dashboard-heading">状态看板</h2>
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading}
          aria-busy={loading}
          aria-label="刷新状态看板"
        >
          {loading ? '加载中…' : '刷新'}
        </button>
      </div>

      {error && (
        <p role="alert" className="dashboard-unavailable">
          <span aria-hidden="true">⚠ </span>
          {error}
          <br />
          <span className="dashboard-last-success">
            上次成功加载：{lastSuccess ? formatTimestamp(lastSuccess.toISOString()) : '尚无'}
          </span>
        </p>
      )}

      {data && (
        <div className="dashboard-cards">
          <section aria-labelledby="orders-heading" className="dashboard-card">
            <h3 id="orders-heading">订单（{data.orders.length}）</h3>
            <ul>
              {data.orders.map((order) => (
                <li key={order.order_id} className="entity-row">
                  <p className="entity-line">
                    <strong>{order.order_id}</strong> · {order.product_id} · 数量{' '}
                    {order.quantity} · 优先级 {order.priority}
                  </p>
                  {order.notes && (
                    <p className="entity-notes">
                      <span className="badge badge-untrusted" aria-label="不受信任内容">
                        <span aria-hidden="true">🛈 </span>untrusted
                      </span>
                      {order.injection_suspected && (
                        <span
                          className="badge badge-injection"
                          aria-label="疑似提示注入"
                        >
                          <span aria-hidden="true">⚠ </span>疑似注入
                        </span>
                      )}{' '}
                      <span className="entity-notes-text">{order.notes}</span>
                    </p>
                  )}
                  <p className="entity-meta">
                    <SourceBadge source={order.source} /> · 更新于{' '}
                    {formatTimestamp(order.last_updated_at)}
                  </p>
                </li>
              ))}
            </ul>
          </section>

          <section aria-labelledby="materials-heading" className="dashboard-card">
            <h3 id="materials-heading">物料（{data.materials.length}）</h3>
            <ul>
              {data.materials.map((material) => (
                <li key={material.material_id} className="entity-row">
                  <p className="entity-line">
                    <strong>{material.material_id}</strong> · {material.name} · 可用{' '}
                    {material.quantity_available} {material.unit}（已预留{' '}
                    {material.reserved_quantity}）
                  </p>
                  <p className="entity-meta">
                    <SourceBadge source={material.source} /> · 更新于{' '}
                    {formatTimestamp(material.last_updated_at)}
                  </p>
                </li>
              ))}
            </ul>
          </section>

          <section aria-labelledby="machines-heading" className="dashboard-card">
            <h3 id="machines-heading">机器（{data.machines.length}）</h3>
            <ul>
              {data.machines.map((machine) => (
                <li key={machine.machine_id} className="entity-row">
                  <p className="entity-line">
                    <strong>{machine.machine_id}</strong> · {machine.machine_type} · 状态{' '}
                    <span className={`machine-status machine-status-${machine.status}`}>
                      {machine.status}
                    </span>
                  </p>
                  <p className="entity-meta">
                    <SourceBadge source={machine.source} /> · 更新于{' '}
                    {formatTimestamp(machine.last_updated_at)}
                  </p>
                </li>
              ))}
            </ul>
          </section>

          <section aria-labelledby="workers-heading" className="dashboard-card">
            <h3 id="workers-heading">工人（{data.workers.length}）</h3>
            <ul>
              {data.workers.map((worker) => (
                <li key={worker.worker_id} className="entity-row">
                  <p className="entity-line">
                    <strong>{worker.worker_id}</strong> · {worker.name}
                  </p>
                  <p className="entity-meta">
                    <SourceBadge source={worker.source} /> · 更新于{' '}
                    {formatTimestamp(worker.last_updated_at)}
                  </p>
                </li>
              ))}
            </ul>
          </section>

          <section aria-labelledby="plans-heading" className="dashboard-card">
            <h3 id="plans-heading">计划（{data.plans.length}）</h3>
            {data.plans.length === 0 ? (
              <p>尚无计划。前往「排产甘特图」生成今日计划。</p>
            ) : (
              <ul>
                {data.plans.map((plan) => (
                  <li key={plan.plan_id} className="entity-row">
                    <p className="entity-line">
                      <strong>{plan.plan_id}</strong> · 状态 {plan.status} · 可行性{' '}
                      {plan.feasibility} · v{plan.plan_version}
                    </p>
                    <p className="entity-meta">
                      <SourceBadge source={plan.source} /> · 创建于{' '}
                      {formatTimestamp(plan.last_updated_at)}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      )}
    </section>
  );
}
