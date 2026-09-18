import { ReactNode, MouseEvent, useEffect, useState } from "react";

type NativeWindow = {
  minimize(): Promise<void>; toggleMaximize(): Promise<void>; close(): Promise<void>;
  startDragging(): Promise<void>; isMaximized(): Promise<boolean>;
  onResized(handler: () => void): Promise<() => void>;
};
const native = (window as unknown as {
  __TAURI__?: { window: { getCurrentWindow(): NativeWindow } };
}).__TAURI__?.window.getCurrentWindow();

export default function DesktopTitlebar({ children }: { children: ReactNode }) {
  const [maximized, setMaximized] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!native) return;
    let active = true;
    let unlisten: (() => void) | undefined;
    const update = () => { void native.isMaximized().then(value => { if (active) setMaximized(value); }).catch(() => {}); };
    update();
    void native.onResized(update).then(stop => { if (active) unlisten = stop; else stop(); }).catch(() => {});
    return () => { active = false; unlisten?.(); };
  }, []);
  async function perform(action: () => Promise<void>) {
    setError("");
    try { await action(); if (native) setMaximized(await native.isMaximized()); }
    catch { setError("窗口操作失败，请通过托盘重新打开窗口。"); }
  }
  const draggable = (event: MouseEvent) => !((event.target as HTMLElement).closest("button, a, input, .top-actions, .window-controls"));
  return <header className={`topbar ${native ? "desktop-topbar" : ""}`}
    onMouseDown={event => { if (native && event.button === 0 && event.detail === 1 && draggable(event)) void perform(() => native.startDragging()); }}
    onDoubleClick={event => { if (native && draggable(event)) void perform(() => native.toggleMaximize()); }}>
    {children}
    {native && <div className="window-controls" aria-label="窗口控制">
      <button aria-label="最小化窗口" title="最小化" onClick={() => void perform(() => native.minimize())}><svg viewBox="0 0 16 16"><path d="M3 8h10" /></svg></button>
      <button aria-label={maximized ? "还原窗口" : "最大化窗口"} title={maximized ? "还原" : "最大化"} onClick={() => void perform(() => native.toggleMaximize())}><svg viewBox="0 0 16 16">{maximized ? <path d="M5 5V3h8v8h-2M3 5h8v8H3z" /> : <path d="M3 3h10v10H3z" />}</svg></button>
      <button className="window-close" aria-label="关闭到托盘" title="关闭到托盘" onClick={() => void perform(() => native.close())}><svg viewBox="0 0 16 16"><path d="m3 3 10 10M13 3 3 13" /></svg></button>
    </div>}
    {error && <span className="window-error" role="alert">{error}</span>}
  </header>;
}
