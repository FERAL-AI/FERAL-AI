import React from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import Shell from './shell/Shell';
import Chat from './pages/Chat';
import Forge from './pages/Forge';
import Devices from './pages/Devices';
import GenUICanvas from './pages/GenUICanvas';
import GlassBrain from './pages/GlassBrain';
import Timeline from './pages/Timeline';
import Flows from './pages/Flows';
import Intents from './pages/Intents';
import Home from './pages/Home';
import Marketplace from './pages/Marketplace';
import Settings from './pages/Settings';
import Setup from './pages/Setup';
import Skills from './pages/Skills';
import Memory from './pages/Memory';
import MemoryContext from './pages/MemoryContext';
import Wiki from './pages/Wiki';
import Identity from './pages/Identity';
import Agents from './pages/Agents';
import Health from './pages/Health';
import Webhooks from './pages/Webhooks';
import Geofences from './pages/Geofences';
import Apps from './pages/Apps';
import AppsPublish from './pages/AppsPublish';
import AppSurface from './pages/AppSurface';
import Pair from './pages/Pair';
import PairShell from './pages/PairShell';
import ChatPanel from './pages/phone/ChatPanel';
import VoicePanel from './pages/phone/VoicePanel';
import VisionAskPanel from './pages/phone/VisionAskPanel';
import PeripheralsPanel from './pages/phone/PeripheralsPanel';
import AppsPanel from './pages/phone/AppsPanel';
import SettingsPanel from './pages/phone/SettingsPanel';
import Oversight from './pages/Oversight';
import Approvals from './pages/Approvals';
import Checkpoints from './pages/Checkpoints';
import Grants from './pages/Grants';
import Console from './pages/Console';
import Jobs from './pages/Jobs';

export default function App() {
  return (
    <Routes>
      {/* Canonical setup. The legacy /setup/legacy route was removed
          in 2026.5.8, the bundled UI's depth-2 SPA routes were broken
          due to relative asset paths, so the legacy wizard was a
          blank page in practice. /setup now has a pairing step (see
          PairStep in Setup.jsx). The CLI wizard `feral setup` is
          unaffected. */}
      <Route path="/setup" element={<Setup />} />
      <Route path="/setup/legacy" element={<Navigate to="/setup" replace />} />
      {/* Unauthenticated browser-node pairing: any phone can land here. */}
      <Route path="/pair" element={<Pair />} />
      <Route path="/pair/:device_id" element={<PairShell />}>
        <Route index element={<Navigate to="chat" replace />} />
        <Route path="chat" element={<ChatPanel />} />
        <Route path="voice" element={<VoicePanel />} />
        <Route path="vision" element={<VisionAskPanel />} />
        <Route path="peripherals" element={<PeripheralsPanel />} />
        <Route path="apps" element={<AppsPanel />} />
        <Route path="apps/:app_id" element={<AppsPanel />} />
        <Route path="settings" element={<SettingsPanel />} />
      </Route>
      <Route element={<Shell />}>
        <Route path="/" element={<Home />} />
        <Route path="/chat" element={<Chat />} />
        <Route path="/forge" element={<Forge />} />
        <Route path="/skills" element={<Skills />} />
        <Route path="/memory" element={<Memory />} />
        <Route path="/memory/context" element={<MemoryContext />} />
        <Route path="/wiki" element={<Wiki />} />
        <Route path="/identity" element={<Identity />} />
        <Route path="/agents" element={<Agents />} />
        <Route path="/health" element={<Health />} />
        <Route path="/webhooks" element={<Webhooks />} />
        <Route path="/geofences" element={<Geofences />} />
        <Route path="/devices" element={<Devices />} />
        <Route path="/canvas" element={<GenUICanvas />} />
        <Route path="/glass-brain" element={<GlassBrain />} />
        <Route path="/oversight" element={<Oversight />} />
        <Route path="/approvals" element={<Approvals />} />
        <Route path="/checkpoints" element={<Checkpoints />} />
        <Route path="/grants" element={<Grants />} />
        <Route path="/console" element={<Console />} />
        <Route path="/jobs" element={<Jobs />} />
        <Route path="/timeline" element={<Timeline />} />
        <Route path="/flows" element={<Flows />} />
        <Route path="/intents" element={<Intents />} />
        <Route path="/marketplace" element={<Marketplace />} />
        <Route path="/apps" element={<Apps />} />
        <Route path="/apps/publish" element={<AppsPublish />} />
        <Route path="/apps/:app_id" element={<AppSurface />} />
        <Route path="/settings" element={<Settings />} />
        {/* /ambient was a second mount of <Home /> that nothing linked
            to, so it could render a duplicate Home under a URL no
            navigation produced. Redirected rather than deleted: the
            path shipped, so a pinned tab or bookmark may still point
            at it, and a redirect lands them on the surface Ambient was
            folded into instead of bouncing off the catch-all. */}
        <Route path="/ambient" element={<Navigate to="/" replace />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
