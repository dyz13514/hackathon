import { NavLink, Route, Routes } from 'react-router-dom';

import { Approval } from './routes/Approval';
import { Dashboard } from './routes/Dashboard';
import { Import } from './routes/Import';
import { NotFound } from './routes/NotFound';
import { TopBar } from './components/TopBar';
import { PlanCompare } from './routes/PlanCompare';
import { Preferences } from './routes/Preferences';
import { Risks } from './routes/Risks';
import { ROUTES } from './routes/routes';
import { Schedule } from './routes/Schedule';
import { Traces } from './routes/Traces';
import { ValueLedger } from './routes/ValueLedger';
import { WhatIf } from './routes/WhatIf';

/**
 * 应用外壳：顶栏 + 主导航 + 路由出口。
 *
 * 12 个视图见 design.md Components §6。骨架阶段只有 `/` 落地，其余按各自任务补齐；
 * `ROUTES` 表把「视图 → 落地任务」的对应关系留在代码里，避免导航与任务清单脱节。
 *
 * 可访问性（R27.9）：导航是 `<nav>` + 链接列表，可键盘到达；当前项除样式外用
 * `aria-current="page"` 传达，不只依赖颜色。
 */
export function App() {
  return (
    <div className="app-shell">
      <header className="app-header">
        <h1>AI 生产排产助手</h1>
      </header>

      {/* 全局降级模式横幅 + 预算告警（任务 11.6，R25.8/R25.9/R25.4），跨所有视图可见。 */}
      <TopBar />

      <nav className="app-nav" aria-label="主导航">
        <ul>
          {ROUTES.filter((route) => route.implemented).map((route) => (
            <li key={route.path}>
              <NavLink
                to={route.path}
                aria-label={route.label}
                className={({ isActive }) => (isActive ? 'nav-link is-active' : 'nav-link')}
              >
                {route.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>

      <main className="app-main">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/schedule" element={<Schedule />} />
          <Route path="/approval" element={<Approval />} />
          <Route path="/traces" element={<Traces />} />
          <Route path="/risks" element={<Risks />} />
          <Route path="/whatif" element={<WhatIf />} />
          <Route path="/import" element={<Import />} />
          <Route path="/preferences" element={<Preferences />} />
          <Route path="/value" element={<ValueLedger />} />
          <Route path="/plans/:a/compare/:b" element={<PlanCompare />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>
    </div>
  );
}
