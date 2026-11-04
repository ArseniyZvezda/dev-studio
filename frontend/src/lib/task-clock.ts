import { onScopeDispose, ref, watch } from 'vue'
import type { TeamTask } from '@/api/types'

type ClockTask = Pick<TeamTask, 'id' | 'status' | 'limit_reset_at' | 'limit_resume_at'>

/** A quiet agent still consumes time. Stop ticking when the task and its limit wait end. */
export function useTaskClock(task: () => ClockTask) {
  const now = ref(Date.now())
  let clock: ReturnType<typeof setInterval> | undefined
  const stop = () => {
    if (clock !== undefined) clearInterval(clock)
    clock = undefined
  }
  watch(
    [() => task().id, () => task().status, () => task().limit_reset_at ?? task().limit_resume_at],
    ([, status, target]) => {
      stop()
      now.value = Date.now()
      if (status === 'in_progress' || target) {
        clock = setInterval(() => (now.value = Date.now()), 1000)
      }
    },
    { immediate: true },
  )
  onScopeDispose(stop)
  return now
}
