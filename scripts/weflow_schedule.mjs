// Pure scheduling policy. No files, accounts, messages or model calls.
export const alignManagedDailyRun = (existing, nowMs, dailyAt = '00:00') => {
  if (!Number.isFinite(nowMs) || nowMs <= 0 || !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(dailyAt)) {
    throw new Error('Invalid managed daily schedule')
  }
  const [hours, minutes] = dailyAt.split(':').map(Number)
  const boundary = new Date(nowMs)
  boundary.setHours(hours, minutes, 0, 0)
  if (boundary.getTime() > nowMs) boundary.setDate(boundary.getDate() - 1)
  const state = existing?.runState || {}
  const last = Number(state.lastTriggeredAt || 0)
  // Preserve a live/interrupted run's retry policy, and today's completed run.
  // The caller enables this policy only before launching its own WeFlow.
  if (state.lastRunStatus === 'running' || last >= boundary.getTime()) return null
  return {
    firstTriggerAt: boundary.getTime(),
    runState: {
      ...state,
      lastTriggeredAt: undefined,
      lastScheduleKey: undefined,
      lastRunStatus: undefined,
      lastError: undefined,
      lastSkipAt: undefined,
      lastSkipReason: undefined,
    },
  }
}
