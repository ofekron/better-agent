import { API } from "../api";
import { trackedFetch } from "../progress/store";

export function markFirstRunWizardSeen(): Promise<Response> {
  return trackedFetch("userPrefs:firstRunWizardSeen", `${API}/api/user-prefs`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ first_run_wizard_done: true }),
  });
}
