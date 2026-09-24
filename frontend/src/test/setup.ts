import '@testing-library/jest-dom/vitest';

/**
 * jsdom 未实现原生 <dialog> 的 showModal/show/close 与 ::backdrop。
 * 这里补一层最小 polyfill，让弹窗的 open 属性、cancel 事件、Esc 行为可测。
 * 仅测试环境使用，不影响生产（浏览器有原生实现）。
 */
if (typeof HTMLDialogElement !== 'undefined') {
  const proto = HTMLDialogElement.prototype;

  proto.showModal = function showModal(this: HTMLDialogElement) {
    this.open = true;
  };
  proto.show = function show(this: HTMLDialogElement) {
    this.open = true;
  };
  proto.close = function close(this: HTMLDialogElement, returnValue?: string) {
    this.open = false;
    if (returnValue !== undefined) this.returnValue = returnValue;
    this.dispatchEvent(new Event('close'));
  };
}
