"""
Comprehensive tests for the FERAL REST API layer.

Covers: dashboard, system info, health, skills, memory, config, identity,
hardware, security, baseline, commands, and node health endpoints.
Uses FastAPI TestClient with mocked BrainState.
"""

from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Lightweight stub for BrainState used by all route modules
# ---------------------------------------------------------------------------

def _make_mock_state():
    """Build a minimal mock that satisfies the attribute access paths used by routes."""
    s = MagicMock()

    # sessions / daemons / devices
    s.sessions = {}
    s.daemons = {}
    s.devices = {}
    s.activity_log = deque()

    # memory (now async; wrap each callable with AsyncMock)
    s.memory.stats = AsyncMock(return_value={"notes": 5, "episodes": 2, "knowledge_triples": 10})
    s.memory.list_recent = AsyncMock(return_value=[])
    s.memory.search = AsyncMock(return_value=[])
    s.memory.save = AsyncMock(return_value={"id": "note-1", "content": "hello"})
    s.memory.delete = AsyncMock(return_value=True)
    s.memory.count = AsyncMock(return_value=0)
    s.memory.knowledge_query = AsyncMock(return_value=[])
    s.memory.knowledge_search = AsyncMock(return_value=[])
    s.memory.knowledge_store = AsyncMock(return_value={"id": "k-1"})
    s.memory.knowledge_about = AsyncMock(return_value=[])
    s.memory.wiki_list_pages = AsyncMock(return_value=[])
    s.memory.wiki_stats = AsyncMock(return_value={"pages": 0})
    s.memory.wiki_get_page = AsyncMock(return_value=None)
    s.memory.wiki_compile = AsyncMock(return_value={"compiled": True})
    s.memory.episode_recent = AsyncMock(return_value=[])
    s.memory.log_recent = AsyncMock(return_value=[])

    # skill_registry
    s.skill_registry.skills = {}

    # config
    s.config.to_client_safe_dict.return_value = {"llm": {"provider": "openai"}, "version": "0.4.0"}
    s.config.setup_complete = True
    s.config.update_settings = MagicMock()

    # audio / realtime / scene / change_detector / perception
    s.audio.available = True
    s.realtime_proxy = None
    s.scene = MagicMock(available=True)
    s.change_detector.stats.return_value = {}
    s.perception = MagicMock()

    # vault / sandbox / policy
    s.vault = MagicMock()
    s.vault.list_keys.return_value = ["KEY_A"]
    s.vault.to_safe_summary.return_value = {"KEY_A": {"fingerprint": "abc123"}}
    s.sandbox = MagicMock(max_tier="active")
    s.policy = MagicMock()
    s.policy._data = {"name": "default"}
    s.policy.to_dict.return_value = {"name": "default"}

    # device_registry
    s.device_registry = MagicMock()
    s.device_registry.stats = {"total_devices": 0}
    s.device_registry.list_devices.return_value = []

    # hardware_mesh
    s.hardware_mesh = MagicMock()
    s.hardware_mesh.connected_nodes = []
    s.hardware_mesh.ledger.get_recent.return_value = []
    s.hardware_mesh.ledger.stats.return_value = {"total": 0}
    s.hardware_mesh.node_health.get_all.return_value = {}
    # HUP v1.3.0 §5.4.4 — Lane 11 device_announce ingest exposes
    # peripherals here for /api/hardware/mesh + Lane 12's Devices page.
    s.hardware_mesh.list_announced_devices.return_value = []

    # mcp
    s.mcp_server = MagicMock()
    s.mcp_client = MagicMock(stats={"connected": 0})

    # channels / skill_gen
    s.channel_manager = MagicMock(stats={"active": 0})
    s.skill_gen = MagicMock(stats={"generated": 0})
    s.skill_gen.get_pending_skills.return_value = []

    # orchestrator
    s.orchestrator = MagicMock()
    s.orchestrator._multi_agent = None
    s.orchestrator.runtime_status = {"status": "idle"}
    # `LLMProvider.switch_provider` is async (audit-r8 brief #07
    # caught the missing `await` in `/api/config/update`); mock it
    # as such so the route can `await` cleanly in tests.
    s.orchestrator.llm = MagicMock()
    s.orchestrator.llm.switch_provider = AsyncMock()

    # identity_workspace
    s.identity_workspace = MagicMock()
    s.identity_workspace.read_soul.return_value = "I am FERAL."
    s.identity_workspace.read_memory.return_value = ""

    # integrations
    s.oauth = MagicMock()
    s.oauth.status.return_value = {}
    s.spotify = MagicMock(connected=False)
    s.home_assistant = MagicMock(connected=False)
    s.notion = MagicMock(connected=False)
    s.event_bus = MagicMock()
    s.event_bus.stats.return_value = {}
    s.marketplace = MagicMock()
    s.marketplace.list_installed.return_value = []

    # sync / wasm / wake_word / taskflows
    s.sync_engine = MagicMock(stats={"running": True, "peer_count": 0})
    s.wasm_sandbox = MagicMock(available=False)
    s.wake_word = MagicMock(enabled=False)
    s.taskflows = MagicMock()
    s.taskflows.stats.return_value = {}

    # baseline
    s.baseline_engine = MagicMock()
    s.baseline_engine.summary.return_value = {"metrics_tracked": 3, "recent_alerts": 0, "categories": ["health"]}
    s.baseline_engine.get_all_baselines.return_value = []
    s.baseline_engine.get_alerts.return_value = []

    # session_handoff / proactive / scheduler
    s.session_handoff = None
    s.proactive = None
    s.scheduler = None

    # demo
    s._demo = None

    # helper
    s.get_sessions_for_daemon = MagicMock(return_value=set())

    return s


@pytest.fixture()
def client():
    """Create a TestClient with BrainState fully mocked out."""
    mock = _make_mock_state()
    with patch("api.state.state", mock), \
         patch("api.routes.dashboard.state", mock), \
         patch("api.routes.config.state", mock), \
         patch("api.routes.skills.state", mock), \
         patch("api.routes.memory.state", mock), \
         patch("api.routes.baseline.state", mock), \
         patch("api.routes.security_and_hardware.state", mock), \
         patch("api.routes.identity_nodes_sync.state", mock), \
         patch("api.routes.devices.state", mock), \
         patch("api.routes.timeline.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def mock_state():
    mock = _make_mock_state()
    with patch("api.state.state", mock), \
         patch("api.routes.dashboard.state", mock), \
         patch("api.routes.config.state", mock), \
         patch("api.routes.skills.state", mock), \
         patch("api.routes.memory.state", mock), \
         patch("api.routes.baseline.state", mock), \
         patch("api.routes.security_and_hardware.state", mock), \
         patch("api.routes.identity_nodes_sync.state", mock), \
         patch("api.routes.devices.state", mock), \
         patch("api.routes.timeline.state", mock):
        yield mock


# ═══════════════════════════════════════════════
#  Health
# ═══════════════════════════════════════════════

class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "version" in body

    def test_health_returns_version(self, client):
        from version import VERSION as __version__
        body = client.get("/health").json()
        assert body["version"] == __version__


# ═══════════════════════════════════════════════
#  Dashboard
# ═══════════════════════════════════════════════

class TestDashboard:
    def test_dashboard_data(self, client):
        r = client.get("/api/dashboard")
        assert r.status_code == 200
        body = r.json()
        assert "devices" in body
        assert "memory" in body
        assert "skills_count" in body

    def test_api_info(self, client):
        r = client.get("/api/info")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "FERAL Brain"
        from version import VERSION as __version__
        assert body["version"] == __version__
        assert "skills" in body

    def test_system_info(self, client):
        r = client.get("/api/system/info")
        assert r.status_code == 200
        body = r.json()
        from version import VERSION as __version__
        assert body["version"] == __version__
        assert "memory" in body
        assert "security" in body
        assert "voice" in body

    def test_activity_empty(self, client):
        r = client.get("/api/activity")
        assert r.status_code == 200
        assert r.json()["entries"] == []


# ═══════════════════════════════════════════════
#  Skills
# ═══════════════════════════════════════════════

class TestSkills:
    def test_list_skills_empty(self, client):
        r = client.get("/skills")
        assert r.status_code == 200
        assert r.json() == []

    def test_pending_skills_empty(self, client):
        r = client.get("/api/skills/pending")
        assert r.status_code == 200
        assert r.json()["pending"] == []

    def test_generate_skill_missing_capability(self, client):
        r = client.post("/api/skills/generate", json={"service": "test"})
        assert r.status_code == 200
        assert "error" in r.json()


# ═══════════════════════════════════════════════
#  Memory
# ═══════════════════════════════════════════════

class TestMemory:
    def test_memory_stats(self, client):
        r = client.get("/internal/memory/stats")
        assert r.status_code == 200
        body = r.json()
        assert body["notes"] == 5

    def test_memory_stats_observability_fields(self, client, mock_state):
        mock_state.memory.stats.return_value = {
            "notes": 5,
            "episodes": 2,
            "knowledge_triples": 10,
            "embedded_chunks": 7,
        }
        mock_state.memory._vec_index = MagicMock(indexed=False)
        embedder = MagicMock()
        embedder.provider_name = "openai"
        mock_state.memory._embed_queue = MagicMock(_embedder=embedder)

        r = client.get("/internal/memory/stats")
        assert r.status_code == 200
        obs = r.json()["observability"]
        assert obs["sqlite_vec_loaded"] is False
        assert obs["embedding_provider"] == "openai"
        assert obs["chunk_count"] == 7
        assert obs["degraded_semantic_search"] is True

    def test_memory_save(self, client):
        r = client.post("/internal/memory/save", json={"content": "test note"})
        assert r.status_code == 200
        assert r.json()["id"] == "note-1"

    def test_memory_save_empty_content(self, client):
        r = client.post("/internal/memory/save", json={"content": ""})
        assert r.status_code == 200
        assert "error" in r.json()

    def test_memory_recent(self, client):
        r = client.get("/internal/memory/recent")
        assert r.status_code == 200

    def test_knowledge_graph(self, client):
        r = client.get("/api/knowledge/graph?limit=10")
        assert r.status_code == 200
        body = r.json()
        assert "nodes" in body
        assert "links" in body

    def test_knowledge_relationship_returns_503_without_graph(self, client, mock_state):
        mock_state.memory.kg = None
        r = client.get("/api/knowledge/relationship?entity_a=alice&entity_b=bob")
        assert r.status_code == 503

    def test_knowledge_relationship_returns_200_with_graph(self, client, mock_state):
        mock_state.memory.kg = MagicMock()
        with patch(
            "memory.enhanced_search.relationship_query",
            return_value={"path": [], "depth": 0},
        ) as rel:
            r = client.get("/api/knowledge/relationship?entity_a=alice&entity_b=bob")
            assert r.status_code == 200
            assert r.json()["depth"] == 0
            rel.assert_called_once()

    def test_knowledge_visualize_returns_503_without_graph(self, client, mock_state):
        mock_state.memory.kg = None
        r = client.get("/api/knowledge/visualize?entity=alice")
        assert r.status_code == 503

    def test_knowledge_visualize_returns_200_with_graph(self, client, mock_state):
        mock_state.memory.kg = MagicMock()
        with patch(
            "memory.enhanced_search.graph_visualization_data",
            return_value={"nodes": [{"id": "n1"}], "edges": []},
        ) as gv:
            r = client.get("/api/knowledge/visualize?entity=alice")
            assert r.status_code == 200
            assert "nodes" in r.json()
            gv.assert_called_once()

    def test_wiki_stats(self, client):
        r = client.get("/api/wiki/stats")
        assert r.status_code == 200


# ═══════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════

class TestConfig:
    def test_get_config(self, client):
        r = client.get("/api/config")
        assert r.status_code == 200
        assert "llm" in r.json()

    def test_update_config(self, client):
        r = client.post("/api/config/update", json={"section": "llm", "key": "model", "value": "gpt-4o"})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_update_config_missing_fields(self, client):
        r = client.post("/api/config/update", json={"section": "", "key": ""})
        assert r.status_code == 200
        assert "error" in r.json()


# ═══════════════════════════════════════════════
#  Identity
# ═══════════════════════════════════════════════

class TestIdentity:
    def test_get_soul(self, client):
        r = client.get("/api/identity/soul")
        assert r.status_code == 200
        assert r.json()["soul"] == "I am FERAL."

    def test_update_soul(self, client):
        r = client.post("/api/identity/soul", json={"content": "new soul"})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_get_memory_md(self, client):
        r = client.get("/api/identity/memory_md")
        assert r.status_code == 200
        assert "memory" in r.json()


# ═══════════════════════════════════════════════
#  Security
# ═══════════════════════════════════════════════

class TestSecurity:
    def test_vault_summary(self, client):
        r = client.get("/api/security/vault")
        assert r.status_code == 200
        assert "keys" in r.json()

    def test_permissions(self, client):
        r = client.get("/api/security/permissions")
        assert r.status_code == 200
        body = r.json()
        assert body["max_tier"] == "active"
        assert "tiers" in body

    def test_update_permissions_invalid_tier(self, client):
        r = client.post("/api/security/permissions/update", json={"max_tier": "admin"})
        assert r.status_code == 200
        assert "error" in r.json()


# ═══════════════════════════════════════════════
#  Hardware
# ═══════════════════════════════════════════════

class TestHardware:
    def test_list_hardware_devices(self, client):
        r = client.get("/api/hardware/devices")
        assert r.status_code == 200
        assert r.json()["devices"] == []

    def test_hardware_mesh_status(self, client):
        r = client.get("/api/hardware/mesh")
        assert r.status_code == 200
        body = r.json()
        # Both keys must always be present so Lane 12's Devices page
        # never has to branch on shape. HUP v1.3.0 §5.4.4 announce
        # ingest writes into ``announced_devices``.
        assert "nodes" in body
        assert "announced_devices" in body
        assert isinstance(body["announced_devices"], list)

    def test_hardware_stats(self, client):
        r = client.get("/api/hardware/stats")
        assert r.status_code == 200


# ═══════════════════════════════════════════════
#  Baseline
# ═══════════════════════════════════════════════

class TestBaseline:
    def test_baseline_summary(self, client):
        r = client.get("/api/baseline/summary")
        assert r.status_code == 200
        body = r.json()
        assert body["metrics_tracked"] == 3

    def test_baseline_metrics(self, client):
        r = client.get("/api/baseline/metrics")
        assert r.status_code == 200
        assert "metrics" in r.json()

    def test_baseline_alerts(self, client):
        r = client.get("/api/baseline/alerts")
        assert r.status_code == 200
        assert r.json()["alerts"] == []

    def test_baseline_summary_no_engine(self, client, mock_state):
        mock_state.baseline_engine = None
        r = client.get("/api/baseline/summary")
        assert r.status_code == 200
        assert r.json()["metrics_tracked"] == 0


# ═══════════════════════════════════════════════
#  Commands & Nodes
# ═══════════════════════════════════════════════

class TestCommandsAndNodes:
    def test_recent_commands(self, client):
        r = client.get("/api/commands/recent")
        assert r.status_code == 200
        body = r.json()
        assert body["commands"] == []
        assert "stats" in body

    def test_nodes_health(self, client):
        r = client.get("/api/nodes/health")
        assert r.status_code == 200
        assert "nodes" in r.json()

    def test_list_nodes(self, client):
        r = client.get("/api/nodes")
        assert r.status_code == 200
        assert r.json()["count"] == 0

    def test_recent_commands_no_mesh(self, client, mock_state):
        mock_state.hardware_mesh = None
        r = client.get("/api/commands/recent")
        assert r.status_code == 200
        assert "error" in r.json()


# ═══════════════════════════════════════════════
#  Setup & Misc
# ═══════════════════════════════════════════════

class TestSetupAndMisc:
    def test_setup_status(self, client):
        r = client.get("/api/setup/status")
        assert r.status_code == 200
        assert "setup_complete" in r.json()

    def test_sync_status(self, client):
        r = client.get("/api/sync/status")
        assert r.status_code == 200

    def test_policy_endpoint(self, client):
        r = client.get("/api/policy")
        assert r.status_code == 200

    def test_identity_greeting(self, client):
        r = client.get("/api/identity/greeting")
        assert r.status_code == 200
        assert "greeting" in r.json()
