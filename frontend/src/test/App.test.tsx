import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import { App } from '../App';
import { ROUTES } from '../routes/routes';

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

describe('App 外壳', () => {
  it('根路径渲染状态看板', () => {
    renderAt('/');
    expect(screen.getByRole('heading', { level: 2, name: 'Dashboard' })).toBeInTheDocument();
  });

  it('未知路径渲染 NotFound 而不是空白页', () => {
    renderAt('/nope');
    expect(screen.getByRole('heading', { level: 2, name: 'Page not found' })).toBeInTheDocument();
  });

  it('导航只列出已落地视图', () => {
    renderAt('/');
    const nav = screen.getByRole('navigation', { name: 'Main navigation' });
    const implemented = ROUTES.filter((route) => route.implemented);
    expect(nav.querySelectorAll('a')).toHaveLength(implemented.length);
  });

  it('当前导航项不只依赖颜色传达状态', () => {
    renderAt('/');
    const active = screen.getByRole('link', { name: 'Dashboard' });
    expect(active).toHaveClass('is-active');
  });
});
