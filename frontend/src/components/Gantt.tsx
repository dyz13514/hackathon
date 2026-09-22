/**
 * 排产甘特图（design.md Components §6 `/schedule` 行，R4 / R8.6）。
 *
 * 横轴时间、纵轴机器。每个作业条标 `order_id` 与工序号；作业条前端的换型段以斜纹
 * （hatch）显示，与加工段区分。
 *
 * 纯展示组件：只接收已排产作业，不发请求、不持状态。时间→像素的换算是纯函数，因此
 * 布局可在测试里逐条断言（`Gantt.test.tsx`）。
 *
 * 可访问性（R27.9）：整图是一个带 `aria-label` 的 `role="img"`，并另附一份对屏幕阅读器
 * 可读的文字摘要（`.gantt-sr-summary`，视觉隐藏）——甘特图的纯图形对读屏器不可用，因此
 * 每条作业同时以文字列出「订单、工序、机器、起止」。换型段除斜纹外在标题里注明分钟数，
 * 不只依赖图案传达。
 */

import type { ScheduledJob } from '../api/plans';

const ROW_HEIGHT = 40;
const BAR_HEIGHT = 24;
const AXIS_HEIGHT = 28;
const LEFT_GUTTER = 96;
const RIGHT_PAD = 16;
const CHART_WIDTH = 900;

export interface GanttProps {
  readonly jobs: readonly ScheduledJob[];
}

interface Row {
  readonly machineId: string;
  readonly jobs: readonly ScheduledJob[];
}

/** 按机器分组，机器按 ID 升序、每台内作业按起始时刻升序（确定性布局）。 */
export function groupByMachine(jobs: readonly ScheduledJob[]): Row[] {
  const byMachine = new Map<string, ScheduledJob[]>();
  for (const job of jobs) {
    const list = byMachine.get(job.machine_id) ?? [];
    list.push(job);
    byMachine.set(job.machine_id, list);
  }
  return [...byMachine.keys()]
    .sort()
    .map((machineId) => ({
      machineId,
      jobs: [...(byMachine.get(machineId) ?? [])].sort((a, b) =>
        a.start_time < b.start_time ? -1 : a.start_time > b.start_time ? 1 : 0,
      ),
    }));
}

/** 全部作业的时间跨度 `[min start, max end]`（毫秒）。空集回退到一个 8 小时窗。 */
export function timeSpan(jobs: readonly ScheduledJob[]): { readonly min: number; readonly max: number } {
  if (jobs.length === 0) {
    const now = 0;
    return { min: now, max: now + 8 * 60 * 60 * 1000 };
  }
  let min = Number.POSITIVE_INFINITY;
  let max = Number.NEGATIVE_INFINITY;
  for (const job of jobs) {
    min = Math.min(min, Date.parse(job.start_time));
    max = Math.max(max, Date.parse(job.end_time));
  }
  if (min === max) {
    max = min + 60 * 60 * 1000;
  }
  return { min, max };
}

function formatClock(iso: string): string {
  const date = new Date(iso);
  const hh = String(date.getHours()).padStart(2, '0');
  const mm = String(date.getMinutes()).padStart(2, '0');
  return `${hh}:${mm}`;
}

export function Gantt({ jobs }: GanttProps) {
  const rows = groupByMachine(jobs);
  const span = timeSpan(jobs);
  const plotWidth = CHART_WIDTH - LEFT_GUTTER - RIGHT_PAD;
  const totalMs = span.max - span.min || 1;
  const height = AXIS_HEIGHT + rows.length * ROW_HEIGHT + 8;

  const xOf = (ms: number) => LEFT_GUTTER + ((ms - span.min) / totalMs) * plotWidth;

  const summary =
    jobs.length === 0
      ? 'The current plan has no scheduled jobs.'
      : jobs
          .map(
            (job) =>
              `Order ${job.order_id} operation ${job.operation_sequence}: machine ${job.machine_id}, ` +
              `worker ${job.worker_id}, ${formatClock(job.start_time)} to ${formatClock(job.end_time)}` +
              (job.changeover_minutes > 0 ? `, incl. ${job.changeover_minutes} min changeover` : ''),
          )
          .join('; ');

  return (
    <figure className="gantt" aria-label="Schedule Gantt chart: time on the horizontal axis, machines on the vertical axis">
      <svg
        role="img"
        aria-label="Schedule Gantt chart"
        width="100%"
        viewBox={`0 0 ${CHART_WIDTH} ${height}`}
        className="gantt-svg"
      >
        <defs>
          {/* 换型段斜纹：除颜色外用图案区分加工与换型（R27.9） */}
          <pattern
            id="changeover-hatch"
            patternUnits="userSpaceOnUse"
            width="6"
            height="6"
            patternTransform="rotate(45)"
          >
            <rect width="6" height="6" fill="#e8a33d" />
            <line x1="0" y1="0" x2="0" y2="6" stroke="#a86a12" strokeWidth="2" />
          </pattern>
        </defs>

        {/* 时间轴刻度：起点与终点两个标签就足够定位（演示窗为单日） */}
        <g className="gantt-axis">
          <line
            x1={LEFT_GUTTER}
            y1={AXIS_HEIGHT}
            x2={CHART_WIDTH - RIGHT_PAD}
            y2={AXIS_HEIGHT}
            stroke="#d5dae0"
          />
          <text x={LEFT_GUTTER} y={AXIS_HEIGHT - 8} fontSize="11" fill="#5b6570">
            {jobs.length > 0 ? formatClock(new Date(span.min).toISOString()) : ''}
          </text>
          <text
            x={CHART_WIDTH - RIGHT_PAD}
            y={AXIS_HEIGHT - 8}
            fontSize="11"
            fill="#5b6570"
            textAnchor="end"
          >
            {jobs.length > 0 ? formatClock(new Date(span.max).toISOString()) : ''}
          </text>
        </g>

        {rows.map((row, rowIndex) => {
          const y = AXIS_HEIGHT + rowIndex * ROW_HEIGHT;
          return (
            <g key={row.machineId} className="gantt-row" data-machine={row.machineId}>
              <text x={8} y={y + ROW_HEIGHT / 2 + 4} fontSize="12" fill="#14171a">
                {row.machineId}
              </text>
              <line
                x1={LEFT_GUTTER}
                y1={y + ROW_HEIGHT}
                x2={CHART_WIDTH - RIGHT_PAD}
                y2={y + ROW_HEIGHT}
                stroke="#eef1f4"
              />
              {row.jobs.map((job) => {
                const startMs = Date.parse(job.start_time);
                const endMs = Date.parse(job.end_time);
                const changeoverMs = job.changeover_minutes * 60 * 1000;
                const changeoverEndMs = Math.min(startMs + changeoverMs, endMs);
                const barY = y + (ROW_HEIGHT - BAR_HEIGHT) / 2;
                const x0 = xOf(startMs);
                const xChangeEnd = xOf(changeoverEndMs);
                const x1 = xOf(endMs);
                const label = `${job.order_id}·OP${job.operation_sequence}`;
                // 纯展示：条太窄时不画标签（此前会留下 "OR" 这类被裁掉的残字），宽度够但放不下
                // 时截断加省略号。估算按 fontSize 11 的粗略字宽（5.5 user unit/字符，viewBox 固定
                // 900 宽，因此与屏幕缩放无关）。完整的 `job_id` 始终在 <title> 里，信息不丢。
                const barWidth = Math.max(x1 - x0, 1);
                const labelChars = Math.max(Math.floor((barWidth - 8) / 5.5), 0);
                const showLabel = labelChars >= 4;
                const labelText =
                  label.length > labelChars
                    ? `${label.slice(0, Math.max(labelChars - 1, 1))}…`
                    : label;
                return (
                  <g
                    key={job.job_id}
                    className="gantt-bar"
                    data-job={job.job_id}
                    data-changeover-minutes={job.changeover_minutes}
                  >
                    <title>
                      {`${job.job_id} · machine ${job.machine_id} · worker ${job.worker_id} · ` +
                        `${formatClock(job.start_time)}–${formatClock(job.end_time)}` +
                        (job.changeover_minutes > 0
                          ? ` · changeover ${job.changeover_minutes} min`
                          : '')}
                    </title>
                    {/* 换型段（斜纹），仅在有换型分钟时绘制 */}
                    {job.changeover_minutes > 0 && xChangeEnd > x0 && (
                      <rect
                        className="gantt-changeover"
                        x={x0}
                        y={barY}
                        width={Math.max(xChangeEnd - x0, 1)}
                        height={BAR_HEIGHT}
                        fill="url(#changeover-hatch)"
                      />
                    )}
                    {/* 加工段 */}
                    <rect
                      className="gantt-process"
                      x={xChangeEnd}
                      y={barY}
                      width={Math.max(x1 - xChangeEnd, 1)}
                      height={BAR_HEIGHT}
                      fill="#10508c"
                      rx="2"
                    />
                    {showLabel && (
                      <text
                        x={x0 + 4}
                        y={barY + BAR_HEIGHT / 2 + 4}
                        fontSize="11"
                        fill="#ffffff"
                      >
                        {labelText}
                      </text>
                    )}
                  </g>
                );
              })}
            </g>
          );
        })}
      </svg>
      <figcaption className="gantt-sr-summary">{summary}</figcaption>
    </figure>
  );
}
