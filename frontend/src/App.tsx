import { NavLink, Route, Routes } from 'react-router-dom';

import { Approval } from './routes/Approval';
import { Dashboard } from './routes/Dashboard';
import { NotFound } from './routes/NotFound';
import { PlanCompare } from './routes/PlanCompare';
import { ROUTES } from './routes/routes';
import { Schedule } from './routes/Schedule';
import { Traces } from './routes/Traces';

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
          <Route path="/plans/:a/compare/:b" element={<PlanCompare />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>
    </div>
  );
}
