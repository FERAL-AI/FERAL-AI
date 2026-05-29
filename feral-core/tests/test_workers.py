"""Tests for FERAL worker modules — skill lists and system prompts."""


class TestHomeWorker:
    def test_skills_list(self):
        from agents.workers.home_worker import HOME_SKILLS

        assert isinstance(HOME_SKILLS, list)
        assert len(HOME_SKILLS) > 0
        # smart_home_hue is the only registered home skill (manifest:
        # skills/manifests/smart_home.json). The previous list named
        # phantom ids (home_assistant / hue_lights / smart_thermostat /
        # door_lock) that have no manifest — guard that they don't return.
        assert "smart_home_hue" in HOME_SKILLS
        for phantom in ("hue_lights", "smart_thermostat", "door_lock", "home_assistant"):
            assert phantom not in HOME_SKILLS, f"{phantom} is not a registered skill"

    def test_prompt_content(self):
        from agents.workers.home_worker import HOME_PROMPT

        assert isinstance(HOME_PROMPT, str)
        assert len(HOME_PROMPT) > 50
        assert "home" in HOME_PROMPT.lower()


class TestHealthWorker:
    def test_skills_list(self):
        from agents.workers.health_worker import HEALTH_SKILLS

        assert isinstance(HEALTH_SKILLS, list)
        assert len(HEALTH_SKILLS) > 0
        assert "health_monitor" in HEALTH_SKILLS

    def test_prompt_content(self):
        from agents.workers.health_worker import HEALTH_PROMPT

        assert isinstance(HEALTH_PROMPT, str)
        assert len(HEALTH_PROMPT) > 50
        assert "health" in HEALTH_PROMPT.lower()


class TestCreativeWorker:
    def test_skills_list(self):
        from agents.workers.creative_worker import CREATIVE_SKILLS

        assert isinstance(CREATIVE_SKILLS, list)
        assert len(CREATIVE_SKILLS) > 0
        assert "spotify" in CREATIVE_SKILLS

    def test_prompt_content(self):
        from agents.workers.creative_worker import CREATIVE_PROMPT

        assert isinstance(CREATIVE_PROMPT, str)
        assert len(CREATIVE_PROMPT) > 50


class TestResearchWorker:
    def test_skills_list(self):
        from agents.workers.research_worker import RESEARCH_SKILLS

        assert isinstance(RESEARCH_SKILLS, list)
        assert len(RESEARCH_SKILLS) > 0
        assert "web_search" in RESEARCH_SKILLS

    def test_prompt_content(self):
        from agents.workers.research_worker import RESEARCH_PROMPT

        assert isinstance(RESEARCH_PROMPT, str)
        assert len(RESEARCH_PROMPT) > 50
        assert "research" in RESEARCH_PROMPT.lower()
