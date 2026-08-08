// ADR 0011 §6 (schedules) — shared delete-schedule mutation helper, native-
// when-open (`delete_schedule` intent -> `schedule_store.delete`, the SAME
// function every legacy delete REST route ultimately calls) with a REST
// fallback when the shared system socket isn't open. `update_schedule`/
// `pause_schedule`/`resume_schedule` do NOT exist as `SystemIntent`s
// (`stores/schedule_store.py` has no update/pause/resume mutation at all —
// `system_commands.py`'s own docstring documents this); every current
// schedule-mutating UI (`SchedulesPage.tsx`, `SessionBackgroundStrip.tsx`)
// only ever cancels, so there is no affordance stranded on legacy for those
// verbs today. One implementation, parameterized by the caller's own REST
// fallback (`SchedulesPage.tsx` uses `deleteScheduleById`,
// `SessionBackgroundStrip.tsx` uses the session-scoped `cancelSchedule`) —
// both legacy routes converge on the same underlying store mutation, so a
// single delete-by-id intent covers either caller.

import { nativeOrRestMutation } from "./nativeOrRestMutation";
import { submitSystemIntent } from "./systemFeedRegistry";

export function deleteScheduleNativeOrRest(
  scheduleId: string,
  restFallback: () => Promise<void>,
): Promise<void> {
  return nativeOrRestMutation(
    submitSystemIntent({ kind: "delete_schedule", schedule_id: scheduleId }),
    restFallback,
  );
}
