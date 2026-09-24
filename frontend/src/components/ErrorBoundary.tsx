/**
 * 应用级错误边界（round-2 功能增强）。
 *
 * 目的：任一路由/组件在渲染中抛出未捕获异常时，不再让用户看到整页空白，
 * 而是显示一个英文的安全回退页，提供恢复入口：
 *  - Try again：重置边界状态，重新渲染当前视图（适合瞬时错误）；
 *  - Go to Dashboard：回到已知可用的首页；
 *  - Reload page：整页刷新（适合已进入不可恢复状态时）。
 *
 * 路由变化后可自愈：`resetKey`（由调用方传入当前 pathname）变化时清空错误状态，
 * 因此用户切换到别的页面即可离开错误态，不会被“钉死”在回退页。
 *
 * 该组件只捕获渲染期错误（React 错误边界的语义）；异步请求错误仍由各视图的
 * try/catch + role="alert" 处理。二者互补。
 */

import { Component, type ErrorInfo, type ReactNode } from 'react';

interface ErrorBoundaryProps {
  /** 值变化时自动清除错误状态（通常传当前路由 pathname）。 */
  readonly resetKey?: string;
  readonly children: ReactNode;
}

interface ErrorBoundaryState {
  readonly error: Error | null;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidUpdate(prevProps: ErrorBoundaryProps): void {
    // 路由变化（或其它 resetKey 变化）后自动脱离错误态。
    if (this.state.error && prevProps.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 仅本地诊断，不外泄任何数据；生产可替换为上报。
    // eslint-disable-next-line no-console
    console.error('Unhandled UI error caught by ErrorBoundary:', error, info.componentStack);
  }

  private handleRetry = (): void => {
    this.setState({ error: null });
  };

  private handleReload = (): void => {
    window.location.reload();
  };

  render(): ReactNode {
    if (this.state.error) {
      return (
        <section role="alert" aria-labelledby="app-error-heading" className="app-error-boundary">
          <h2 id="app-error-heading">Something went wrong</h2>
          <p>
            This page hit an unexpected error and could not be displayed. Your data was not changed. You
            can try again, go back to the dashboard, or reload the page.
          </p>
          <div className="app-error-actions">
            <button type="button" onClick={this.handleRetry}>
              Try again
            </button>
            <a className="app-error-link" href="/">
              Go to Dashboard
            </a>
            <button type="button" onClick={this.handleReload}>
              Reload page
            </button>
          </div>
        </section>
      );
    }
    return this.props.children;
  }
}
