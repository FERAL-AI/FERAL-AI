from __future__ import annotations

import re
import uuid

from models.protocol import FeralMessage, SDUIPayload
from security.safety_resolver import is_read_only

GREETINGS = {
    "hello", "hey", "hi", "sup", "yo", "howdy", "good morning", "good evening",
    "good afternoon", "good night", "what's up", "whats up", "how are you",
    "how you doing", "how's it going", "hows it going", "greetings",
}


async def direct_execute(orchestrator, session_id: str, text: str, skills):
    cleaned = text.strip().lower().rstrip("!.,?")
    if cleaned in GREETINGS:
        await orchestrator._send_text(
            session_id,
            "Hey! I'm running in direct mode right now (no LLM connected). "
            "Connect a model in AI Providers for task reasoning. Some explicitly matched read-only skills can still run."
        )
        return

    if not skills:
        all_skills = list(orchestrator.skills.skills.values())
        sdui = {
            "type": "VStack",
            "spacing": 16,
            "padding": 20,
            "children": [
                {"type": "Text", "value": "FERAL Brain", "style": "headline", "color": "#6c5ce7"},
                {"type": "Text", "value": f'No matching skill for: "{text}"', "style": "body"},
                {"type": "Divider"},
                {"type": "Text", "value": "Available Skills:", "style": "subtitle"},
                *[
                    {
                        "type": "Card",
                        "corner_radius": 12,
                        "children": [
                            {
                                "type": "Text",
                                "value": skill.brand.name,
                                "style": "subtitle",
                                "color": skill.brand.primary_color,
                            },
                            {"type": "Text", "value": skill.description, "style": "caption"},
                            {
                                "type": "Text",
                                "value": f'Try: "{skill.trigger_phrases[0]}"' if skill.trigger_phrases else "",
                                "style": "caption",
                            },
                        ],
                    }
                    for skill in all_skills
                ],
                {"type": "Divider"},
                {
                    "type": "Badge",
                    "label": "Direct Mode — Set OPENAI_API_KEY for full agent reasoning",
                    "color": "#fdcb6e",
                    "text_color": "#2d3436",
                },
            ],
        }
        await orchestrator.send(
            session_id,
            FeralMessage(
                session_id=session_id,
                hop="brain",
                type="sdui",
                payload=SDUIPayload(root=sdui).model_dump(),
            ),
        )
        return

    # A semantic/tool bag is not an instruction. After an LLM failure it can
    # contain core tools unrelated to the requested task; never execute its
    # first entry or guess between endpoints.
    candidates = {}
    command = re.sub(r"^please\s+", "", text.strip(), flags=re.IGNORECASE)
    for candidate in skills:
        matches = [
            phrase.strip()
            for phrase in candidate.trigger_phrases
            if phrase.strip()
            and re.match(re.escape(phrase.strip()) + r"(?:\b|$)", command, re.IGNORECASE)
        ]
        if matches:
            candidates[candidate.skill_id] = (max(map(len, matches)), candidate)
    longest = max((score for score, _ in candidates.values()), default=0)
    matched = [candidate for score, candidate in candidates.values() if score == longest]
    if len(matched) != 1:
        await orchestrator._send_text(
            session_id,
            "I couldn't safely identify that action without a working model. "
            "No fallback action has run. Connect a model in AI Providers and try again.",
        )
        return
    skill = matched[0]

    if skill.requires_daemon:
        await orchestrator._send_text(
            session_id, "Computer actions need a working model and an exact device review. No fallback action has run."
        )
        return

    if len(skill.endpoints) != 1:
        await orchestrator._send_text(
            session_id, "That action needs a working model to select its operation. No fallback action has run."
        )
        return
    endpoint = skill.endpoints[0]
    tool_name = f"{skill.skill_id}__{endpoint.id}"
    if not is_read_only(tool_name, registry=orchestrator.skills, strict=True):
        await orchestrator._send_text(
            session_id, "That action needs a working model and its normal review. No fallback action has run."
        )
        return
    args = extract_args_from_text(text, endpoint)
    if any(param.required and param.name not in args for param in endpoint.params):
        await orchestrator._send_text(session_id, "I need more details for that action. No fallback action has run.")
        return
    runner = getattr(orchestrator, "tool_runner", None)
    if runner is None:
        await orchestrator._send_text(session_id, "The action dispatcher is unavailable. No fallback action has run.")
        return
    result = await runner.execute_tool_call_for_llm(
        session_id,
        {"id": f"direct-{uuid.uuid4().hex}", "name": tool_name, "args": args},
        [skill],
    )

    if not isinstance(result, dict):
        await orchestrator._send_text(session_id, "The action returned an invalid response; completion is unconfirmed.")
        return
    if result.get("status") == "pending_approval":
        await orchestrator._send_text(
            session_id, "That action was sent for your approval. Review its approval card for the current status."
        )
        return
    if result.get("success") is True and result.get("data") is not None:
        sdui = orchestrator.genui.generate(
            data=result["data"],
            skill_brand=skill.brand.model_dump(),
            ui_hint=endpoint.ui_hint,
            endpoint_id=endpoint.id,
        )
        await orchestrator.send(
            session_id,
            FeralMessage(
                session_id=session_id,
                hop="brain",
                type="sdui",
                payload=SDUIPayload(root=sdui).model_dump(),
            ),
        )
    else:
        await orchestrator._send_text(
            session_id,
            f"That action did not report completion: {str(result.get('error') or result.get('status') or 'unknown result')[:500]}",
        )


async def handle_memory_direct(orchestrator, session_id: str, text: str, _skill):
    if not orchestrator.memory:
        await orchestrator._send_text(session_id, "Memory system not available.")
        return

    text_lower = text.lower()

    list_patterns = ["my notes", "recent notes", "recent memories", "list notes", "show notes", "show memories", "all notes"]
    search_patterns = ["recall", "what did i", "what was", "find ", "search notes", "search memories"]
    save_patterns = ["remember", "save", "write down", "don't forget", "store", "note that", "note this"]
    knowledge_patterns = ["i am ", "my name is ", "i live ", "i work ", "i like ", "my favorite"]

    if any(pattern in text_lower for pattern in knowledge_patterns):
        await orchestrator.memory.knowledge_store(
            subject="user",
            predicate="stated",
            obj=text[:300],
            source="conversation",
        )
        sdui = {
            "type": "VStack",
            "spacing": 16,
            "padding": 20,
            "children": [
                {
                    "type": "HStack",
                    "spacing": 10,
                    "children": [
                        {"type": "Icon", "name": "brain", "size": 28, "color": "#a29bfe"},
                        {"type": "Text", "value": "Learned", "style": "headline", "color": "#a29bfe"},
                    ],
                },
                {"type": "Text", "value": f"I'll remember: {text[:200]}", "style": "body"},
            ],
        }
        await orchestrator.send(
            session_id,
            FeralMessage(
                session_id=session_id,
                hop="brain",
                type="sdui",
                payload=SDUIPayload(root=sdui).model_dump(),
            ),
        )
        return

    if any(pattern in text_lower for pattern in list_patterns):
        results = await orchestrator.memory.list_recent(limit=5)
        if results:
            sdui = {
                "type": "VStack",
                "spacing": 12,
                "padding": 20,
                "children": [
                    {
                        "type": "HStack",
                        "spacing": 10,
                        "children": [
                            {"type": "Icon", "name": "note.text", "size": 22, "color": "#FDCB6E"},
                            {
                                "type": "Text",
                                "value": f"Recent Notes ({len(results)})",
                                "style": "headline",
                                "color": "#FDCB6E",
                            },
                        ],
                    },
                    {"type": "Divider"},
                    *[
                        {
                            "type": "Card",
                            "corner_radius": 10,
                            "children": [
                                {"type": "Text", "value": item["content"], "style": "body"},
                                {"type": "Badge", "label": f"ID: {item['id']}", "color": "#636e72"},
                            ],
                        }
                        for item in results
                    ],
                ],
            }
        else:
            sdui = {
                "type": "VStack",
                "spacing": 16,
                "padding": 20,
                "children": [
                    {"type": "Text", "value": "No notes yet", "style": "headline", "color": "#FDCB6E"},
                    {"type": "Text", "value": "Say 'remember that...' to save your first note.", "style": "body"},
                ],
            }
        await orchestrator.send(
            session_id,
            FeralMessage(
                session_id=session_id,
                hop="brain",
                type="sdui",
                payload=SDUIPayload(root=sdui).model_dump(),
            ),
        )
        return

    if any(pattern in text_lower for pattern in search_patterns):
        query = text
        for phrase in ["recall ", "what did i save about ", "search notes for ", "search memories for ", "find "]:
            if text_lower.startswith(phrase):
                query = text[len(phrase) :].strip()
                break

        results = await orchestrator.memory.search(query=query, limit=5)
        if results:
            sdui = {
                "type": "VStack",
                "spacing": 12,
                "padding": 20,
                "children": [
                    {"type": "Text", "value": f"Found {len(results)} memories", "style": "headline", "color": "#FDCB6E"},
                    {"type": "Divider"},
                    *[
                        {
                            "type": "Card",
                            "corner_radius": 10,
                            "children": [
                                {"type": "Text", "value": item["content"], "style": "body"},
                                {
                                    "type": "HStack",
                                    "spacing": 8,
                                    "children": [
                                        {"type": "Badge", "label": f"ID: {item['id']}", "color": "#636e72"},
                                        {"type": "Badge", "label": item["importance"], "color": "#6c5ce7"},
                                    ],
                                },
                            ],
                        }
                        for item in results
                    ],
                ],
            }
        else:
            sdui = {
                "type": "VStack",
                "spacing": 16,
                "padding": 20,
                "children": [
                    {"type": "Text", "value": f'No memories found for: "{query}"', "style": "body"},
                    {"type": "Text", "value": "Try saving something first.", "style": "caption"},
                ],
            }
        await orchestrator.send(
            session_id,
            FeralMessage(
                session_id=session_id,
                hop="brain",
                type="sdui",
                payload=SDUIPayload(root=sdui).model_dump(),
            ),
        )
        return

    if any(pattern in text_lower for pattern in save_patterns):
        content = text
        for phrase in ["remember that ", "remember ", "save a note ", "save note ", "note that ", "note ", "write down ", "don't forget "]:
            if text_lower.startswith(phrase):
                content = text[len(phrase) :].strip()
                break

        result = await orchestrator.memory.save(content=content, source="voice")
        sdui = {
            "type": "VStack",
            "spacing": 16,
            "padding": 20,
            "children": [
                {
                    "type": "HStack",
                    "spacing": 10,
                    "children": [
                        {"type": "Icon", "name": "checkmark.circle.fill", "size": 28, "color": "#00b894"},
                        {"type": "Text", "value": "Saved to Memory", "style": "headline", "color": "#00b894"},
                    ],
                },
                {"type": "Divider"},
                {
                    "type": "Card",
                    "corner_radius": 12,
                    "children": [
                        {"type": "Text", "value": result["content"], "style": "body"},
                        {
                            "type": "HStack",
                            "spacing": 8,
                            "children": [
                                {"type": "Badge", "label": f"ID: {result['id']}", "color": "#636e72"},
                                {"type": "Badge", "label": result["importance"], "color": "#6c5ce7"},
                            ],
                        },
                    ],
                },
                {"type": "Text", "value": f"Total memories: {await orchestrator.memory.count()}", "style": "caption"},
            ],
        }
        await orchestrator.send(
            session_id,
            FeralMessage(
                session_id=session_id,
                hop="brain",
                type="sdui",
                payload=SDUIPayload(root=sdui).model_dump(),
            ),
        )
        return

    # Fallback
    results = await orchestrator.memory.list_recent(limit=5)
    sdui = {
        "type": "VStack",
        "spacing": 12,
        "padding": 20,
        "children": [
            {
                "type": "HStack",
                "spacing": 10,
                "children": [
                    {"type": "Icon", "name": "note.text", "size": 22, "color": "#FDCB6E"},
                    {"type": "Text", "value": f"Recent Notes ({len(results)})", "style": "headline", "color": "#FDCB6E"},
                ],
            },
            {"type": "Divider"},
            *(
                [
                    {
                        "type": "Card",
                        "corner_radius": 10,
                        "children": [
                            {"type": "Text", "value": item["content"], "style": "body"},
                            {"type": "Badge", "label": f"ID: {item['id']}", "color": "#636e72"},
                        ],
                    }
                    for item in results
                ]
                if results
                else [{"type": "Text", "value": "No notes yet. Say 'remember that...' to start.", "style": "body"}]
            ),
        ],
    }
    await orchestrator.send(
        session_id,
        FeralMessage(
            session_id=session_id,
            hop="brain",
            type="sdui",
            payload=SDUIPayload(root=sdui).model_dump(),
        ),
    )


async def handle_daemon_direct(orchestrator, session_id: str, text: str, _skill):
    if not orchestrator.daemons:
        await orchestrator._send_text(session_id, "No daemon connected.")
        return

    node_id = list(orchestrator.daemons.keys())[0]
    text_lower = text.lower()

    executor = "shell"
    action = ""

    app_map = {
        "chrome": "Google Chrome",
        "safari": "Safari",
        "terminal": "Terminal",
        "vscode": "Visual Studio Code",
        "code": "Visual Studio Code",
        "spotify": "Spotify",
        "finder": "Finder",
        "notes": "Notes",
        "messages": "Messages",
        "slack": "Slack",
        "discord": "Discord",
        "firefox": "Firefox",
        "arc": "Arc",
        "iterm": "iTerm",
    }

    for keyword, app_name in app_map.items():
        if keyword in text_lower and ("open" in text_lower or "launch" in text_lower):
            executor = "applescript"
            action = f'tell application "{app_name}" to activate'
            break

    if not action:
        if "volume" in text_lower or "mute" in text_lower:
            if "mute" in text_lower:
                action = "osascript -e 'set volume output muted true'"
            elif "up" in text_lower or "higher" in text_lower or "louder" in text_lower:
                action = "osascript -e 'set volume output volume ((output volume of (get volume settings)) + 15)'"
            elif "down" in text_lower or "lower" in text_lower or "quieter" in text_lower:
                action = "osascript -e 'set volume output volume ((output volume of (get volume settings)) - 15)'"
            else:
                nums = re.findall(r"\d+", text)
                vol = nums[0] if nums else "50"
                action = f"osascript -e 'set volume output volume {vol}'"
        elif "lock" in text_lower:
            action = "pmset displaysleepnow"
        elif "screenshot" in text_lower or "screen" in text_lower:
            action = "screencapture -x /tmp/feral_screenshot.png && echo 'Screenshot saved'"
        elif "dark mode" in text_lower:
            executor = "applescript"
            action = 'tell application "System Events" to tell appearance preferences to set dark mode to not dark mode'
        elif "run" in text_lower:
            await orchestrator._send_text(session_id, "Direct shell commands are disabled for safety. Use a skill or ask me to help.")
            return

    if not action:
        await orchestrator._send_text(session_id, f"Matched Desktop Control but couldn't parse: '{text}'")
        return

    await orchestrator._send_text(session_id, f"Sending to daemon: [{executor}] {action[:80]}...")
    await orchestrator._execute_daemon_command(
        session_id,
        f"daemon_{node_id}",
        executor,
        {"command": action, "script": action},
    )


def extract_args_from_text(text: str, endpoint) -> dict:
    args = {}
    stop_words = {
        "the",
        "in",
        "at",
        "for",
        "what",
        "is",
        "whats",
        "what's",
        "weather",
        "how",
        "get",
        "show",
        "me",
        "my",
        "of",
        "a",
        "an",
        "please",
        "can",
        "you",
        "tell",
        "about",
        "find",
        "search",
    }
    content_words = [word for word in text.split() if word.lower() not in stop_words]
    subject = " ".join(content_words) if content_words else text

    for param in endpoint.params:
        if param.default is not None:
            args[param.name] = param.default
        if param.name in ("q", "query", "text", "search", "message"):
            args[param.name] = text
        elif param.name in ("city", "location", "place", "address"):
            args[param.name] = subject or text
        elif param.name == "lat" and "lon" in [p.name for p in endpoint.params]:
            pass
    return args
