/**
 * PlanModeBanner renders the per-session plan-mode posture.
 *
 * Plan mode is a posture the user chose and cannot see anywhere else: in
 * it the agent researches and proposes but every mutating tool call is
 * refused at dispatch. Without a visible marker the mode reads as the
 * agent malfunctioning ("why won't it edit the file?"), which is why the
 * brain emits a frame for it at all.
 *
 * Frame shape (agents/orchestrator.py::_emit_plan_mode_frame, which
 * forwards PlanModeState.describe plus, on exit, `approved`):
 *   {
 *     session_id, plan_mode: bool, entered_at: float|null,
 *     reason: string, entered_by: "user"|"api"|string,
 *     plan_count: number, latest_plan: object|null, approved?: bool
 *   }
 *
 * Pinned and replaced in place, exactly like <TodoPanel>: the frame is a
 * full state snapshot rather than a delta, so the latest one always
 * describes the session completely.
 *
 * The frame is emitted on TRANSITIONS ONLY. A client that connects while
 * a session is already in plan mode receives nothing (verified against a
 * live brain: connecting to /v1/session on a plan-mode session yields no
 * `plan_mode` frame). Chat.jsx therefore also hydrates this from
 * GET /api/sessions/{id}/plan_mode when the active session changes;
 * without that, a reload silently drops the banner while the mode is
 * still on.
 */
import React from 'react';
import { ClipboardList } from 'lucide-react';
import Glass from '../ui/Glass';

/**
 * Narrow the frame payload to what the banner renders, tolerating the
 * partial shapes older brains emit. Exported for the vitest.
 */
export function normalizePlanMode(raw) {
  if (!raw || typeof raw !== 'object') return null;
  if (raw.plan_mode !== true) return null;
  const count = Number(raw.plan_count);
  return {
    reason: typeof raw.reason === 'string' ? raw.reason : '',
    enteredBy: typeof raw.entered_by === 'string' ? raw.entered_by : '',
    planCount: Number.isFinite(count) && count > 0 ? count : 0,
  };
}

export default function PlanModeBanner({ state }) {
  const plan = normalizePlanMode(state);
  if (!plan) return null;

  return (
    <Glass
      level={0}
      radius="md"
      padding="sm"
      className="v2-planmode"
      data-testid="plan-mode-banner"
    >
      <div className="v2-planmode__head">
        <ClipboardList size={13} aria-hidden="true" className="v2-planmode__icon" />
        <span className="v2-planmode__title">Plan mode</span>
        {plan.planCount > 0 && (
          <span className="v2-planmode__meta" data-testid="plan-mode-count">
            {plan.planCount} submitted
          </span>
        )}
      </div>
      <p className="v2-planmode__body">
        Researching only. Mutating tools are refused until you leave plan
        mode with <code>/plan approve</code> or <code>/plan off</code>.
        Approving does not pre-approve the individual steps.
      </p>
      {plan.reason && (
        <p className="v2-planmode__reason" data-testid="plan-mode-reason">
          {plan.reason}
        </p>
      )}
    </Glass>
  );
}
