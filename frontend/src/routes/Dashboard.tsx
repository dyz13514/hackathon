/**
 * 状态看板占位视图。
 *
 * 真正的五类实体卡片区（Order / Material / Machine / Worker / Plan，含 `source` 徽章与
 * `last_updated_at`）在任务 3.7 落地，数据来自 `GET /api/state/dashboard`。
 * 此处**刻意不放假数据**：骨架里的假数据会在后续任务中被误当成真实链路。
 */
export function Dashboard() {
  return (
    <section aria-labelledby="dashboard-heading">
      <h2 id="dashboard-heading">状态看板</h2>
      <p>骨架已就位。五类实体的当前状态在任务 3.7 接入 `GET /api/state/dashboard` 后显示。</p>
    </section>
  );
}
