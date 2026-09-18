import { useEffect, useState } from "react";
import { api, post } from "./api";

type Policy = {
  ai_enhancement_enabled: boolean;
  mode: string;
  warning: string | null;
  format_capabilities?: {
    native_groups: { label: string; extensions: string[] }[];
    visual_review_formats: string[];
    local_converter_formats: string[];
    local_converter_available: boolean;
    optional_converter_formats: string[];
    optional_converter_available: boolean;
  };
};

export default function DocumentParsingSettings() {
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    api<Policy>("/api/settings/document-parsing").then(result => {
      if (active) setPolicy(result.data);
    }).catch(error => { if (active) setMessage(String(error)); });
    return () => { active = false; };
  }, []);
  async function useLocal() {
    setBusy(true);
    try {
      const result = await post<Policy>("/api/settings/document-parsing", { ai_enhancement_enabled: false });
      setPolicy(result.data);
      setMessage(result.summary);
    } catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  }
  return <section className="panel">
    <header className="panel-head"><h2>文档解析</h2><span>LOCAL FIRST</span></header>
    <div className="panel-body">
      <p><strong>{!policy ? "正在读取设置…" : policy.ai_enhancement_enabled ? "检测到显式增强配置" : "本地解析 · AI 增强已关闭"}</strong></p>
      <p className="panel-note">Word 提取段落与表格；Excel 提取工作表、公式、合并单元格和图表标签；PDF 提取文字、页码、目录、批注与链接。图片会登记格式、尺寸及已有的本地标题/说明/关键词，但不会假装已经看懂像素内容。</p>
      {policy?.format_capabilities && <details className="boundary-note"><summary>本机格式覆盖与边界</summary>
        {policy.format_capabilities.native_groups.map(group => <p key={group.label}><strong>{group.label}</strong>：{group.extensions.join("、")}</p>)}
        <p><strong>扫描件/视觉补全</strong>：{policy.format_capabilities.visual_review_formats.join("、")}；本地会登记图片格式、尺寸和已有嵌入说明，扫描文字和图像语义仍须显式启用视觉处理。</p>
        <p><strong>本地旧Office转换</strong>：{policy.format_capabilities.local_converter_formats.join("、")}；{policy.format_capabilities.local_converter_available ? "LibreOffice已可用，会在明确入库时无窗口临时转换。" : "未发现LibreOffice，暂不转换。"}</p>
        <p><strong>其他可选转换</strong>：{policy.format_capabilities.optional_converter_formats.join("、")}；{policy.format_capabilities.optional_converter_available ? "Docling已可用。" : "当前未安装Docling，不会假装已读到正文。"}</p>
      </details>}
      <div className="boundary-note"><strong>扫描件与图片视觉补全：尚未接入</strong><p>当前可检索图片来源、格式、尺寸及已有的本地标题/说明/关键词；不会读取截图文字、表格截图或图像语义，也不读取位置、设备或人脸信息。本地解析不会调用文档大模型，也不会下载解析模型；它不影响已独立运行的 Luna 资料理解、Embedding、重排或 Agent。</p></div>
      <button role="switch" aria-checked={false} disabled title="视觉补全尚未接入">视觉补全：关闭（暂不可用）</button>{" "}
      <button disabled={busy || !policy} onClick={useLocal}>{busy ? "保存中…" : "保持本地解析并保存"}</button>
      {policy?.warning && <p role="alert">{policy.warning}</p>}
      {message && <p role="status">{message}</p>}
    </div>
  </section>;
}
