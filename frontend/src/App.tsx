import { useCallback, useEffect, useState } from 'react';
import { NavLink, Route, Routes } from 'react-router-dom';

import { Approval } from './routes/Approval';
import { Dashboard } from './routes/Dashboard';
import { Import } from './routes/Import';
import { Insights } from './routes/Insights';
import { NotFound } from './routes/NotFound';
import { TopBar } from './components/TopBar';
import { LoginModal } from './components/LoginModal';
import { PlanCompare } from './routes/PlanCompare';
import { Preferences } from './routes/Preferences';
import { Quote } from './routes/Quote';
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
 *
 * 全局认证：监听 `login-required` 事件弹出登录框，登录成功后派发 `login-succeeded`
 * 通知 client.ts 的 waitForLogin 继续重试原始请求（R23.12）。
 */
export function App() {
  const [loginOpen, setLoginOpen] = useState(false);

  // 监听 client.ts 触发的 login-required 事件
  useEffect(() => {
    const handler = () => setLoginOpen(true);
    window.addEventListener('login-required', handler);
    return () => window.removeEventListener('login-required', handler);
  }, []);

  const handleLoginSuccess = useCallback(() => {
    setLoginOpen(false);
    // 通知 client.ts 的 waitForLogin 继续重试
    window.dispatchEvent(new CustomEvent('login-succeeded'));
  }, []);

  return (
    <div className="app-shell">
      <LoginModal open={loginOpen} onSuccess={handleLoginSuccess} />
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
          <Route path="/insights" element={<Insights />} />
          <Route path="/quote" element={<Quote />} />
          <Route path="/plans/:a/compare/:b" element={<PlanCompare />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>
    </div>
  );
}
