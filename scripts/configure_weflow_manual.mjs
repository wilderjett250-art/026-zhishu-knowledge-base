import { existsSync, readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
import { createHash } from 'node:crypto'
import path from 'node:path'

const [action, argument] = process.argv.slice(2)
const weflowRoot = String(process.env.WEFLOW_ROOT || '').trim()
const configDir = String(process.env.WEFLOW_CONFIG_DIR || '').trim()
if (!weflowRoot || !configDir) throw new Error('WeFlow paths are required')

const storeEntry = path.join(weflowRoot, 'node_modules', 'electron-store', 'index.js')
if (!existsSync(storeEntry)) throw new Error('WeFlow electron-store is unavailable')
const { default: Store } = await import(pathToFileURL(storeEntry).href)
const store = new Store({ name: 'WeFlow-config', cwd: configDir })

const dbPath = String(store.get('dbPath', '') || '').trim()
const myWxid = String(store.get('myWxid', '') || '').trim()
const scopeKey = (dbPath || myWxid) ? `${dbPath}::${myWxid}` : 'default'
const automationMap = store.get('exportAutomationTaskMap', {}) || {}
const currentItem = automationMap[scopeKey]
const existingTasks = Array.isArray(currentItem?.tasks) ? currentItem.tasks : []

if (action === 'configure') {
  const request = JSON.parse(readFileSync(argument, 'utf8'))
  const requestedIds = Array.isArray(request.sessionIds)
    ? [...new Set(request.sessionIds.map(value => String(value || '').trim()).filter(Boolean))]
    : []
  if (!request.taskId || requestedIds.length === 0 || requestedIds.length > 1000) {
    throw new Error('Manual export request is invalid')
  }

  const countCacheMap = store.get('exportSessionMessageCountCacheMap', {}) || {}
  const rawCounts = countCacheMap[scopeKey]?.counts
  if (!rawCounts || typeof rawCounts !== 'object') {
    throw new Error('WeFlow session cache is unavailable')
  }
  const sessionIds = requestedIds.filter(id => Number(rawCounts[id] || 0) > 0)
  if (sessionIds.length === 0) throw new Error('No authorized sessions are exportable')

  const contactsMap = store.get('contactsListCacheMap', {}) || {}
  const contacts = Array.isArray(contactsMap[scopeKey]?.contacts)
    ? contactsMap[scopeKey].contacts
    : []
  const names = new Map(contacts.map(item => [String(item?.username || ''), String(item?.displayName || item?.remark || item?.nickname || item?.username || '')]))
  const outputDir = String(store.get('exportPath', '') || '').trim()
  if (!outputDir) throw new Error('WeFlow export directory is unavailable')

  const now = Date.now()
  const task = {
    id: String(request.taskId),
    name: 'PKAS 手动同步',
    enabled: true,
    sessionIds,
    sessionNames: sessionIds.map(id => names.get(id) || id),
    outputDir,
    schedule: { type: 'interval', intervalDays: 0, intervalHours: 1, firstTriggerAt: now - 1000 },
    // A manual sync is an explicit range replay. It must not be silently
    // converted into the scheduler's "only when newer" optimization because
    // the caller needs a fresh export artifact as acceptance evidence.
    condition: { type: 'manual-range-replay' },
    stopCondition: { maxRuns: 1 },
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
        start: String(request.start || ''),
        end: String(request.end || ''),
      },
    },
    runState: {
      lastRunStatus: 'idle',
      successCount: 0,
      lastSuccessAt: Math.max(0, Number(request.lastWatermarkMs || 0)) || undefined,
    },
    createdAt: now,
    updatedAt: now,
  }
  const tasks = existingTasks
    .map(item => (item?.id === 'pkas-weflow-daily-v1' ? { ...item, enabled: false, updatedAt: now } : item))
    .filter(item => !String(item?.id || '').startsWith('pkas-weflow-manual-'))
    .concat(task)
  store.set('exportAutomationTaskMap', { ...automationMap, [scopeKey]: { updatedAt: now, tasks } })
  console.log(JSON.stringify({ status: 'configured', task_id: task.id, session_count: sessionIds.length, secret_fields_accessed: false }))
} else if (action === 'status') {
  const task = existingTasks.find(item => item?.id === argument)
  if (!task) throw new Error('Manual export task was not found')
  const reason = String(task.runState?.lastSkipReason || '')
  const rawError = String(task.runState?.lastError || '')
  const errorCode = !rawError
    ? null
    : /导出参数|option|parameter/i.test(rawError)
      ? 'invalid_options'
      : /数据库|database|wcdb/i.test(rawError)
        ? 'database'
        : /permission|denied|eacces|eperm|权限/i.test(rawError)
          ? 'permission'
          : /enoent|路径|目录|file|path/i.test(rawError)
            ? 'filesystem'
            : /ipc|invoke|channel/i.test(rawError)
              ? 'ipc'
              : /memory|heap|内存/i.test(rawError)
                ? 'resource'
                : /timeout|超时/i.test(rawError)
                  ? 'timeout'
                  : 'unknown'
  console.log(JSON.stringify({
    status: 'ok',
    task_id: String(task.id),
    run_status: String(task.runState?.lastRunStatus || 'idle'),
    skip_code: reason.includes('无新消息') ? 'no_new_messages' : (reason ? 'other' : null),
    error_present: Boolean(rawError),
    error_code: errorCode,
    error_fingerprint: rawError ? createHash('sha256').update(rawError).digest('hex').slice(0, 16) : null,
    exported_session_count: Math.max(0, Number(task.runState?.lastExportedSessionCount || 0)),
    failed_session_count: Math.max(0, Number(task.runState?.lastFailedSessionCount || 0)),
    no_data_session_count: Math.max(0, Number(task.runState?.lastNoDataSessionCount || 0)),
    secret_fields_accessed: false,
  }))
} else if (action === 'cleanup') {
  const now = Date.now()
  const tasks = existingTasks.filter(item => item?.id !== argument)
  store.set('exportAutomationTaskMap', { ...automationMap, [scopeKey]: { updatedAt: now, tasks } })
  console.log(JSON.stringify({ status: 'cleaned', removed: tasks.length !== existingTasks.length, secret_fields_accessed: false }))
} else if (action === 'audit') {
  console.log(JSON.stringify({
    status: 'ok',
    enabled_task_count: existingTasks.filter(item => item?.enabled === true).length,
    pkas_enabled_task_count: existingTasks.filter(item => item?.enabled === true && String(item?.id || '').startsWith('pkas-')).length,
    secret_fields_accessed: false,
  }))
} else if (action === 'disable-pkas-legacy') {
  const now = Date.now()
  let changed = 0
  const tasks = existingTasks.map(item => {
    if (item?.enabled !== true || !String(item?.id || '').startsWith('pkas-')) return item
    changed += 1
    return { ...item, enabled: false, updatedAt: now }
  })
  store.set('exportAutomationTaskMap', { ...automationMap, [scopeKey]: { updatedAt: now, tasks } })
  console.log(JSON.stringify({ status: 'disabled', changed, secret_fields_accessed: false }))
} else {
  throw new Error('Unsupported action')
}
