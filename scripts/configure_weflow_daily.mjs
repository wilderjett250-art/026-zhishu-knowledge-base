import { existsSync, mkdirSync, statfsSync } from 'node:fs'
import { homedir } from 'node:os'
import { pathToFileURL } from 'node:url'
import path from 'node:path'
import { alignManagedDailyRun } from './weflow_schedule.mjs'

const weflowRoot = String(process.env.WEFLOW_ROOT || '').trim()
if (!weflowRoot) {
  throw new Error('WEFLOW_ROOT must be explicitly configured for a legacy WeFlow daily export')
}
const defaultAppData = process.env.APPDATA || path.join(homedir(), 'AppData', 'Roaming')
const configDir = process.env.WEFLOW_CONFIG_DIR || path.join(defaultAppData, 'weflow')
const taskId = 'pkas-weflow-daily-v1'
const taskName = '每日微信消息增量同步'
const legacyTaskName = '每6小时微信消息增量同步'
const requestedDailyAt = String(process.env.WEFLOW_DAILY_AT || '').trim()
const resetDailyAnchor = process.env.WEFLOW_RESET_DAILY_ANCHOR === '1'

const resolveLocalDailyAnchor = (nowMs, value) => {
  const match = /^(?:[01]\d|2[0-3]):[0-5]\d$/.exec(value)
  if (!match) return 0
  const [hours, minutes] = value.split(':').map(Number)
  const now = new Date(nowMs)
  const today = new Date(now)
  today.setHours(hours, minutes, 0, 0)
  const elapsedSinceTodayAnchor = nowMs - today.getTime()
  // When the installer runs just after midnight, use that same midnight;
  // otherwise anchor the next run to the next local midnight.
  if (elapsedSinceTodayAnchor >= 0 && elapsedSinceTodayAnchor <= 5 * 60 * 1000) {
    return today.getTime()
  }
  if (elapsedSinceTodayAnchor < 0) return today.getTime()
  today.setDate(today.getDate() + 1)
  return today.getTime()
}

const electronStoreEntry = path.join(weflowRoot, 'node_modules', 'electron-store', 'index.js')
if (!existsSync(electronStoreEntry)) {
  throw new Error('WeFlow electron-store dependency was not found')
}

const { default: Store } = await import(pathToFileURL(electronStoreEntry).href)
const store = new Store({ name: 'WeFlow-config', cwd: configDir })

// Read only the non-secret fields needed to locate the active account cache.
// Values are deliberately never printed.
const dbPath = String(store.get('dbPath', '') || '').trim()
const myWxid = String(store.get('myWxid', '') || '').trim()
const scopeKey = (dbPath || myWxid) ? `${dbPath}::${myWxid}` : 'default'

const countCacheMap = store.get('exportSessionMessageCountCacheMap', {}) || {}
const countItem = countCacheMap[scopeKey]
const rawCounts = countItem && typeof countItem === 'object' ? countItem.counts : null
if (!rawCounts || typeof rawCounts !== 'object') {
  throw new Error('Active WeFlow session-count cache is unavailable; open the Export page once and retry')
}

const contactsCacheMap = store.get('contactsListCacheMap', {}) || {}
const contactsItem = contactsCacheMap[scopeKey]
const contacts = Array.isArray(contactsItem?.contacts) ? contactsItem.contacts : []
const allowedTypes = new Set(['friend', 'group', 'former_friend'])
const contactById = new Map(
  contacts
    .filter((item) => item && allowedTypes.has(String(item.type || '')))
    .map((item) => [String(item.username || '').trim(), item])
    .filter(([id]) => Boolean(id)),
)

// Match WeFlow's own automation-creation rule: only conversation sessions that
// currently contain at least one message are included. At run time, WeFlow's
// new-message condition and one-day range skip unchanged/empty conversations.
const sessionIds = Object.entries(rawCounts)
  .filter(([id, count]) => contactById.has(String(id).trim()) && Number(count) > 0)
  .map(([id]) => String(id).trim())
  .sort()

if (sessionIds.length === 0) {
  throw new Error('No exportable conversation sessions were found in the active WeFlow cache')
}

const sessionNames = sessionIds.map((id) => {
  const contact = contactById.get(id) || {}
  return String(contact.displayName || contact.remark || contact.nickname || id).trim() || id
})

const now = Date.now()
const automationMap = store.get('exportAutomationTaskMap', {}) || {}
const currentItem = automationMap[scopeKey]
const existingTasks = Array.isArray(currentItem?.tasks) ? currentItem.tasks : []
const existing = existingTasks.find((task) => (
  task?.id === taskId || task?.name === taskName || task?.name === legacyTaskName
))
const requestedRoot = String(process.env.PKAS_WEFLOW_EXPORT_ROOT || '').trim()
const priorManagedRoot = String(existing?.outputDir || '').trim()
const stableManagedRoot = path.basename(priorManagedRoot).toLowerCase() === 'pkas-weflow-exports'
  ? priorManagedRoot : ''
// A scheduled runner may switch from I: to G: when a drive is unavailable.
const outputDir = requestedRoot || stableManagedRoot || String(store.get('exportPath', '') || '').trim()
if (!outputDir || !path.isAbsolute(outputDir)) {
  throw new Error('An absolute WeFlow export directory is required')
}
if (requestedRoot && path.basename(requestedRoot).toLowerCase() !== 'pkas-weflow-exports') {
  throw new Error('PKAS managed export directory must end in PKAS-WeFlow-Exports')
}
mkdirSync(outputDir, { recursive: true })
const exportSpace = statfsSync(outputDir)
if (Number(exportSpace.bavail) * Number(exportSpace.bsize) < 2 * 1024 ** 3) {
  throw new Error('Managed WeFlow export drive has less than 2 GiB free')
}
const hasExistingAnchor = Number(existing?.schedule?.firstTriggerAt || 0) > 0
const requestedAnchor = resetDailyAnchor && requestedDailyAt
  ? resolveLocalDailyAnchor(now, requestedDailyAt)
  : 0
const dueInvocation = process.env.PKAS_WEFLOW_FORCE_DUE === '1'
  ? alignManagedDailyRun(existing, now, requestedDailyAt || '00:00')
  : null
const runState = dueInvocation ? dueInvocation.runState : requestedAnchor > 0
  ? {
      ...(existing?.runState || {}),
      lastTriggeredAt: undefined,
      lastScheduleKey: undefined,
      lastRunStatus: undefined,
      lastSkipAt: undefined,
      lastSkipReason: undefined,
    }
  : existing?.runState

const task = {
  id: existing?.id || taskId,
  name: taskName,
  enabled: true,
  sessionIds,
  sessionNames,
  outputDir,
  schedule: {
    type: 'interval',
    intervalDays: 1,
    intervalHours: 0,
    // firstTriggerAt retains the chosen wall-clock time for a calendar-aware
    // producer; lastTriggeredAt records whether this day's slot was consumed.
    // A legacy producer can still use these fields as its interval anchor.
    firstTriggerAt: dueInvocation ? dueInvocation.firstTriggerAt : requestedAnchor > 0
      ? requestedAnchor
      : (hasExistingAnchor ? existing?.schedule?.firstTriggerAt : now),
  },
  condition: {
    type: 'new-message-since-last-success',
  },
  template: {
    scope: 'multi',
    optionTemplate: {
      format: 'excel',
      fileNamingMode: 'date-range',
      exportMedia: false,
      exportAvatars: false,
      exportImages: false,
      exportVoices: false,
      exportVideos: false,
      exportEmojis: false,
      exportFiles: false,
      exportVoiceAsText: false,
      excelCompactColumns: false,
      sessionLayout: 'per-session',
      sessionNameWithTypePrefix: true,
      displayNamePreference: 'remark',
      exportConcurrency: 2,
    },
    dateRangeConfig: {
      version: 1,
      preset: 'custom',
      useAllTime: false,
      relativeMode: 'last-n-days',
      relativeDays: 1,
    },
  },
  runState,
  createdAt: Number(existing?.createdAt) > 0 ? Number(existing.createdAt) : now,
  updatedAt: now,
}

const tasks = existingTasks
  .filter((item) => (
    item?.id !== task.id && item?.name !== taskName && item?.name !== legacyTaskName
  ))
  .concat(task)

store.set('exportAutomationTaskMap', {
  ...automationMap,
  [scopeKey]: {
    updatedAt: now,
    tasks,
  },
})

console.log(JSON.stringify({
  status: 'configured',
  task_id: task.id,
  enabled: task.enabled,
  interval_days: task.schedule.intervalDays,
  interval_hours: task.schedule.intervalHours,
  schedule_mode: 'daily',
  lookback_days: task.template.dateRangeConfig.relativeDays,
  condition: task.condition.type,
  session_count: sessionIds.length,
  invocation_aligned: Boolean(dueInvocation),
  secret_fields_accessed: false,
}))
