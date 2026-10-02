export type SyncStageTiming = { id: string; label: string; value: string };

const validDuration = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0 && value <= 86_400_000;

export function formatSyncDuration(value: unknown): string {
  if (!validDuration(value)) return "未记录";
  const seconds = Math.floor(value / 1000);
  if (seconds === 0) return "不到1秒";
  if (seconds < 60) return `${seconds}秒`;
  if (seconds < 3600) {
    return `${Math.floor(seconds / 60)}分${seconds % 60 ? ` ${seconds % 60}秒` : ""}`;
  }
  const minutes = Math.floor((seconds % 3600) / 60);
  return `${Math.floor(seconds / 3600)}小时${minutes ? ` ${minutes}分` : ""}`;
}

export function syncTimingStages(counts?: Record<string, unknown> | null): SyncStageTiming[] {
  if (!counts || !["weflow_export_wait_ms", "weflow_import_ms", "local_refresh_ms", "worker_total_ms"]
    .some(key => validDuration(counts[key]))) return [];
  return [
    { id: "export", label: "导出等待", value: counts.weflow_enabled === 0
      ? "未启用" : formatSyncDuration(counts.weflow_export_wait_ms) },
    { id: "import", label: "聊天入库", value: counts.weflow_enabled === 0
      ? "未启用" : formatSyncDuration(counts.weflow_import_ms) },
    { id: "local", label: "资料刷新", value: counts.full_sync_due === 0
      ? "本次无需刷新" : formatSyncDuration(counts.local_refresh_ms) },
  ];
}
