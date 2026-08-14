/**
 * PerceptionShare — UI for the browser-based perception share.
 *
 * Two modes
 * ---------
 *   <PerceptionShare />              → full pane for the Devices / Home page
 *   <PerceptionShare.FloatingChip /> → sticky floating indicator the v2 shell
 *                                      pins to the dock while streaming
 *
 * Accepts no props. All state comes from usePerceptionShare().
 */
import React from 'react';
import {
  Camera, CameraOff, Mic, MicOff, Play, Square, Pause, AlertTriangle,
  Activity, Eye,
} from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import StatusDot from '../ui/StatusDot';
import usePerceptionShare from '../hooks/usePerceptionShare';

const STATUS_LABEL = {
  idle: 'Not sharing',
  requesting: 'Requesting camera…',
  running: 'Sharing',
  paused: 'Paused',
  error: 'Error',
};

const STATUS_TONE = {
  idle: 'neutral',
  requesting: 'warn',
  running: 'live',
  paused: 'warn',
  error: 'error',
};

function useLiveShare() {
  return usePerceptionShare();
}

export default function PerceptionShare() {
  const share = useLiveShare();
  const {
    status, error, stats, controls,
    start, stop, pause, resume, setFps, toggleAudio, toggleVideo,
  } = share;

  const isRunning = status === 'running';
  const isPaused = status === 'paused';

  return (
    <div data-testid="perception-share-pane">
    <Pane
      title="Share my camera"
      actions={<StatusDot tone={STATUS_TONE[status]} pulse={isRunning} />}
    >
      <p className="v2-p v2-p--muted">
        Grants FERAL live video + audio from this browser. Works on any phone
        without installing the native app — open this page, tap start, approve
        the permission prompt. The Brain treats this like a HUP daemon named
        <code> {share.nodeId}</code>.
      </p>

      {error && (
        <Glass level={1} radius="md" padding="sm" className="v2-chip v2-chip--error" style={{ marginBottom: 8, display: 'flex', gap: 6, alignItems: 'center' }}>
          <AlertTriangle size={13} /> {error}
        </Glass>
      )}

      <div className="v2-perception-toolbar" style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        {!isRunning && !isPaused && (
          <button type="button" className="v2-btn v2-btn--primary" onClick={start} data-testid="perception-start">
            <Play size={12} /> Start sharing
          </button>
        )}
        {isRunning && (
          <button type="button" className="v2-btn" onClick={pause} data-testid="perception-pause">
            <Pause size={12} /> Pause
          </button>
        )}
        {isPaused && (
          <button type="button" className="v2-btn v2-btn--primary" onClick={resume} data-testid="perception-resume">
            <Play size={12} /> Resume
          </button>
        )}
        {(isRunning || isPaused) && (
          <button type="button" className="v2-btn" onClick={stop} data-testid="perception-stop">
            <Square size={12} /> Stop
          </button>
        )}
        <div className="v2-perception-controls" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <button
            type="button"
            className={`v2-btn v2-btn--ghost${controls.videoMuted ? ' is-active' : ''}`}
            onClick={toggleVideo}
            aria-pressed={controls.videoMuted}
            title="Toggle video"
          >
            {controls.videoMuted ? <CameraOff size={12} /> : <Camera size={12} />}
          </button>
          <button
            type="button"
            className={`v2-btn v2-btn--ghost${controls.audioMuted ? ' is-active' : ''}`}
            onClick={toggleAudio}
            aria-pressed={controls.audioMuted}
            title="Toggle audio"
          >
            {controls.audioMuted ? <MicOff size={12} /> : <Mic size={12} />}
          </button>
          <label className="v2-p v2-p--muted" style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            <span>fps</span>
            <select
              value={controls.fps}
              onChange={(e) => setFps(Number(e.target.value))}
              className="v2-btn v2-btn--ghost"
              aria-label="Frames per second"
              data-testid="perception-fps"
            >
              {[1, 2, 3, 5].map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
        </div>
      </div>

      <div className="v2-perception-stats" style={{ display: 'flex', gap: 10, marginTop: 10, flexWrap: 'wrap' }}>
        <Glass level={0} radius="md" padding="sm">
          <div className="v2-stat-label">Status</div>
          <div className="v2-stat-value"><Activity size={12} /> {STATUS_LABEL[status]}</div>
        </Glass>
        <Glass level={0} radius="md" padding="sm">
          <div className="v2-stat-label">Frames sent</div>
          <div className="v2-stat-value"><Eye size={12} /> {stats.framesSent}</div>
        </Glass>
        <Glass level={0} radius="md" padding="sm">
          <div className="v2-stat-label">Audio chunks</div>
          <div className="v2-stat-value"><Mic size={12} /> {stats.audioChunksSent}</div>
        </Glass>
      </div>

      <p className="v2-p v2-p--tiny v2-p--muted" style={{ marginTop: 8 }}>
        Privacy: sharing only starts after you click. The indicator is always visible while streaming.
        Frames are capped at 512 KiB and dropped by the Brain above that. We auto-pause if this tab is
        hidden longer than 60 seconds.
      </p>
    </Pane>
    </div>
  );
}


function FloatingChip() {
  const share = useLiveShare();
  if (share.status !== 'running') return null;
  return (
    <div
      className="v2-perception-chip"
      role="status"
      aria-live="polite"
      data-testid="perception-floating-chip"
      style={{
        position: 'fixed',
        right: 20,
        bottom: 92,
        zIndex: 50,
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '8px 12px',
        borderRadius: 999,
        // Recording-red. Semantically this is the alert state (the camera is
        // live and the user needs to see that at a glance), so it reads from
        // --v2-state-error rather than a stray Tailwind red-600.
        background: 'rgb(var(--v2-state-error-rgb) / 0.9)',
        color: 'var(--v2-on-accent)',
        fontSize: 12,
        fontWeight: 600,
        boxShadow: 'var(--v2-shadow-glass-soft)',
      }}
    >
      <span className="v2-perception-chip-dot" style={{
        width: 8,
        height: 8,
        borderRadius: '50%',
        background: 'var(--v2-on-accent)',
        animation: 'pulse 1s ease-in-out infinite',
      }} />
      Sharing camera · {share.controls.fps}fps
      <button
        type="button"
        className="v2-perception-chip-stop"
        onClick={share.stop}
        aria-label="Stop sharing camera"
        style={{
          marginLeft: 4,
          border: 'none',
          background: 'var(--v2-well)',
          color: 'var(--v2-on-accent)',
          cursor: 'pointer',
          padding: '2px 8px',
          borderRadius: 999,
          fontSize: 11,
          fontWeight: 500,
        }}
      >
        Stop
      </button>
    </div>
  );
}

PerceptionShare.FloatingChip = FloatingChip;
