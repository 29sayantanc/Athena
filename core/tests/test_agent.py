"""Agent tests with a scripted fake model and a fake shell. Run: python -m unittest discover -s tests -v"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["ATHENA_HOME"] = tempfile.mkdtemp(prefix="athena-test-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from athena_core import screen, win32  # noqa: E402
from athena_core.agent import Agent  # noqa: E402
from athena_core.calibration import CoordMapper, parse_points  # noqa: E402
from athena_core.config import Settings  # noqa: E402
from athena_core.hooks import HotkeyMatcher  # noqa: E402
from athena_core.llm import LLMClient, strip_think  # noqa: E402
from athena_core.memory import Memory  # noqa: E402
from athena_core.screen import Element  # noqa: E402


class FakeEnv:
    def __init__(self, settings, confirm_answer=True, click=None):
        self.settings = settings
        self.log = []
        self.confirm_answer = confirm_answer
        self.click = click

    async def capture(self):
        f = screen.capture(1344, 28)
        f.elements = [Element(1, "button", "Reply", (100, 100, 400, 160)),
                      Element(2, "edit", "Message body", (100, 300, 1200, 700)),
                      Element(3, "button", "Send", (100, 720, 180, 760))]
        f.window = {"title": "Inbox - Gmail", "process": "chrome.exe"}
        self.log.append(("capture",))
        return f

    async def say(self, text, wait=False):
        self.log.append(("say", text, wait))

    async def show_popup(self, title, markdown, kind="info", spoken=""):
        self.log.append(("popup", title, markdown, kind))

    def chat(self, role, text):
        self.log.append(("chat", role, text))

    async def highlight(self, items, duration, keep, sticky=False):
        self.log.append(("highlight", items, duration, keep))

    async def clear_highlights(self):
        self.log.append(("clear",))

    async def confirm(self, action, details):
        self.log.append(("confirm", action))
        return self.confirm_answer

    async def wait_click(self, timeout):
        self.log.append(("wait_click", timeout))
        return self.click

    def set_state(self, s, d=""):
        self.log.append(("state", s))

    def begin_control(self): self.log.append(("begin_control",))
    def pause_control(self): self.log.append(("pause_control",))
    def input_done(self): pass
    def feedback(self, kind, x, y): self.log.append(("feedback", kind, x, y))
    def caption(self, text, kind="status"): self.log.append(("caption", text, kind))

    def kinds(self):
        return [e[0] for e in self.log]


def native_reply(*calls, content=""):
    return {"choices": [{"message": {"role": "assistant", "content": content, "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
        for i, (n, a) in enumerate(calls)]}}]}


def text_reply(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


class ScriptedModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def handler(self, request: httpx.Request):
        body = json.loads(request.content)
        self.requests.append(body)
        r = self.responses.pop(0)
        if isinstance(r, tuple):
            return httpx.Response(r[0], text=r[1])
        return httpx.Response(200, json=r)


def make(responses, safety="confirm_risky", tool_mode="auto", **env_kw):
    s = Settings(Path(os.environ["ATHENA_HOME"]) / f"s{id(responses)}.json")
    s.update({"model": {"active_provider": "lmstudio", "providers": {"lmstudio": {"model": "qwen2.5-vl-7b"}}, "tool_mode": tool_mode},
              "control": {"safety_mode": safety, "settle_ms": 0, "mouse_speed": "instant"}})
    model = ScriptedModel(responses)
    llm = LLMClient(s)
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(model.handler))
    env = FakeEnv(s, **env_kw)
    mem = Memory(Path(os.environ["ATHENA_HOME"]) / f"m{id(responses)}.json")
    agent = Agent(s, llm, env, CoordMapper(Path(os.environ["ATHENA_HOME"]) / f"c{id(responses)}.json"), mem)
    return agent, env, model


def run(coro):
    return asyncio.run(coro)


class AgentTests(unittest.TestCase):
    def setUp(self):
        win32.ACTIONS.clear()

    def test_short_answer_is_spoken(self):
        agent, env, model = make([native_reply(("reply", {"speech": "It's 4 pm in Tokyo."}))])
        run(self._go(agent, env, "what time is it in Tokyo"))
        self.assertIn(("say", "It's 4 pm in Tokyo.", True), env.log)
        self.assertNotIn("popup", env.kinds())
        # first request carries a screenshot + tools
        req = model.requests[0]
        self.assertIn("tools", req)
        parts = req["messages"][1]["content"]
        self.assertTrue(any(p["type"] == "image_url" for p in parts))
        self.assertIn("[3] button \"Send\"", parts[0]["text"])

    def test_long_plain_text_goes_to_popup(self):
        long = "Here is the plan:\n\n- " + "\n- ".join(f"step {i} does a thing" for i in range(30))
        agent, env, _ = make([text_reply("<think>let me plan this carefully</think>" + long)])
        run(self._go(agent, env, "plan my week"))
        popup = [e for e in env.log if e[0] == "popup"][0]
        self.assertIn("step 29", popup[2])
        self.assertNotIn("think", popup[2])
        spoken = [e for e in env.log if e[0] == "say"][-1][1]
        self.assertIn("popup", spoken)
        self.assertLess(len(spoken.split()), 40)

    def test_gmail_flow_with_explicit_send(self):
        agent, env, model = make([
            native_reply(("say", {"text": "Scrolling up to read the thread."}),
                         ("scroll", {"direction": "up", "amount": 5})),
            native_reply(("click", {"element": 2, "description": "message body"}),
                         ("type_text", {"text": "Hi Priya,\n\nThursday works great.\n\nBest,\nAlex"})),
            native_reply(("click", {"element": 3, "description": "Send button", "risky": True})),
            native_reply(("take_screenshot", {"reason": "verify sent"})),
            native_reply(("reply", {"speech": "Sent."})),
        ])
        run(self._go(agent, env, "reply saying thursday works and send it"))
        self.assertNotIn("confirm", env.kinds())          # user said "send"
        acts = [a[0] for a in win32.ACTIONS]
        self.assertIn("scroll", acts)
        self.assertEqual(win32.get_clipboard(), "")        # clipboard restored after paste
        self.assertIn(("keys", "ctrl+v"), win32.ACTIONS)
        click_targets = [a[3] for a in win32.ACTIONS if a[0] == "click"]
        self.assertIn((140, 740), click_targets)           # Send element centre
        # after screen-changing steps the model received fresh screenshots
        self.assertGreaterEqual(env.kinds().count("capture"), 4)
        # images trimmed to keep_images=3
        last = model.requests[-1]["messages"]
        n_img = sum(1 for m in last if isinstance(m.get("content"), list)
                    for p in m["content"] if p.get("type") == "image_url")
        self.assertLessEqual(n_img, 3)

    def test_risky_click_without_permission_asks_and_respects_denial(self):
        agent, env, model = make([
            native_reply(("click", {"element": 3, "description": "Send button"}),
                         ("take_screenshot", {})),
            native_reply(("reply", {"speech": "Okay, I left it as a draft."})),
        ], confirm_answer=False)
        run(self._go(agent, env, "draft a reply saying yes"))
        self.assertIn("confirm", env.kinds())
        self.assertFalse(any(a[0] == "click" for a in win32.ACTIONS))
        tool_msgs = [m for m in model.requests[1]["messages"] if m["role"] == "tool"]
        self.assertTrue(tool_msgs[0]["content"].startswith("denied"))
        self.assertIn("not run", tool_msgs[1]["content"])

    def test_tutor_mode_highlight_and_wait(self):
        agent, env, model = make([
            native_reply(("say", {"text": "First, open the Image menu."}),
                         ("highlight", {"items": [{"element": 1, "shape": "arrow", "label": "1. Image"}],
                                        "wait_for_click": True})),
            native_reply(("reply", {"speech": "Nice, that's it."})),
        ], click=(150, 120, "left"))
        run(self._go(agent, env, "how do I adjust colours in photoshop"))
        hl = [e for e in env.log if e[0] == "highlight"][0]
        self.assertEqual(hl[1][0]["rect"], [100, 100, 400, 160])
        self.assertEqual(hl[1][0]["label"], "1. Image")
        self.assertIn("wait_click", env.kinds())
        tool_msgs = [m for m in model.requests[1]["messages"] if m["role"] == "tool"]
        self.assertIn("on the highlighted target", tool_msgs[1]["content"])
        self.assertFalse(win32.ACTIONS)  # never touched the mouse

    def test_json_mode_fallback_when_tools_rejected(self):
        agent, env, model = make([
            (400, '{"error":"This model does not support tools / function calling"}'),
            text_reply('<think>user wants a joke</think>```json\n{"thought":"easy","actions":[{"tool":"reply","args":{"speech":"Why did the cursor blush? It saw the mouse move."}}]}\n```'),
            text_reply('{"actions":[{"tool":"reply","args":{"speech":"Again!"}}]}'),
        ])
        run(self._go(agent, env, "tell me a joke"))
        self.assertNotIn("tools", model.requests[1])
        self.assertIn("RESPONSE FORMAT", model.requests[1]["messages"][0]["content"])
        self.assertTrue(any(e[0] == "say" and "blush" in e[1] for e in env.log))
        run(self._go(agent, env, "another"))  # stays in JSON mode, history renders as text
        self.assertNotIn("tools", model.requests[2])
        roles = [m["role"] for m in model.requests[2]["messages"]]
        for a, b in zip(roles, roles[1:]):
            self.assertFalse(a == b == "user", roles)

    def test_leaked_qwen_tool_call_text_is_parsed(self):
        agent, env, _ = make([text_reply('<tool_call>\n{"name": "reply", "arguments": {"speech": "Done."}}\n</tool_call>')])
        run(self._go(agent, env, "hi"))
        self.assertIn(("say", "Done.", True), env.log)

    def test_cancel_keeps_history_valid(self):
        agent, env, model = make([
            native_reply(("wait", {"seconds": 5}), ("reply", {"speech": "late"})),
            native_reply(("reply", {"speech": "ok"})),
        ])

        async def go():
            f = await env.capture()
            t = asyncio.ensure_future(agent.run("do a slow thing", f))
            await asyncio.sleep(0.2)
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            await agent.run("never mind", await env.capture())
        run(go())
        msgs = model.requests[1]["messages"]
        tool_ids = {m["tool_call_id"] for m in msgs if m["role"] == "tool"}
        self.assertEqual(tool_ids, {"c0", "c1"})

    def test_coordinates_in_norm1000_for_qwen3(self):
        agent, env, _ = make([native_reply(("click", {"x": 500, "y": 500, "description": "centre"})),
                              native_reply(("reply", {"speech": "ok"}))])
        agent.settings.update({"model": {"providers": {"lmstudio": {"model": "qwen/qwen3-vl-30b"}}}})
        run(self._go(agent, env, "click the middle"))
        click = [a for a in win32.ACTIONS if a[0] == "click"][0]
        self.assertAlmostEqual(click[3][0], 960, delta=3)
        self.assertAlmostEqual(click[3][1], 540, delta=3)

    def test_followup_reuses_earlier_screenshot(self):
        agent, env, model = make([
            native_reply(("reply", {"speech": "That's the Reply button."})),
            native_reply(("click", {"element": 1, "description": "Reply"}), ("take_screenshot", {})),
            native_reply(("reply", {"speech": "Done."})),
        ])

        async def go():
            await agent.run("what's this button", await env.capture())
            self.assertTrue(agent.is_followup(180))
            n_caps = env.kinds().count("capture")
            await agent.run("ok click it", None, {"title": "Inbox - Gmail", "process": "chrome.exe"})
            return n_caps
        n_before = run(go())
        follow_msg = [m for m in model.requests[1]["messages"] if m["role"] == "user"][-1]
        texts = " ".join(p.get("text", "") for p in follow_msg["content"])
        self.assertIn("no new screenshot", texts)
        self.assertFalse(any(p["type"] == "image_url" for p in follow_msg["content"]))
        # the earlier screenshot is still in context, and element ids still resolve
        self.assertTrue(any(p.get("type") == "image_url" for m in model.requests[1]["messages"]
                            if isinstance(m.get("content"), list) for p in m["content"]))
        self.assertIn((250, 130), [a[3] for a in win32.ACTIONS if a[0] == "click"])
        self.assertEqual(env.kinds().count("capture"), n_before + 1)  # only the model's own take_screenshot

    def test_popup_details_written_as_message_text(self):
        details = "## Steps\n1. Open Image menu\n2. Choose Adjustments then Curves\n3. Drag the midpoint up to brighten the photo"
        agent, env, _ = make([native_reply(("reply", {"speech": "I've put the steps in a popup."}), content=details)])
        run(self._go(agent, env, "how do I brighten this"))
        popup = [e for e in env.log if e[0] == "popup"]
        self.assertTrue(popup and "Curves" in popup[0][2])

    def test_popup_mentioned_but_empty_gets_details_written(self):
        steps = "## Curves\n1. Press **Ctrl+M** to open Curves\n2. Drag the midpoint up to brighten\n3. Pick the Red channel to warm the image"
        agent, env, model = make([
            native_reply(("reply", {"speech": "Curves is quickest, details are in the popup."})),
            text_reply("<think>write it out</think>" + steps),
        ])
        run(self._go(agent, env, "how do I fix the colours"))
        self.assertEqual(len(model.requests), 2)
        self.assertNotIn("tools", model.requests[1])                    # plain writing call, no tools
        self.assertIn("complete, detailed answer", json.dumps(model.requests[1]["messages"][-1]))
        self.assertEqual([e[2] for e in env.log if e[0] == "popup"], [steps])

    def test_popup_that_repeats_the_speech_is_replaced(self):
        speech = "Use Curves to brighten the photo; the full steps are in the popup."
        full = "## Steps\n1. Image → Adjustments → Curves (Ctrl+M)\n2. Drag the middle point upward\n3. Click OK, or use an adjustment layer to keep it editable"
        agent, env, model = make([
            native_reply(("reply", {"speech": speech, "popup_markdown": "Use Curves to brighten the photo. The full steps are in the popup!"})),
            text_reply(full),
        ])
        run(self._go(agent, env, "how do I brighten this"))
        self.assertEqual([e[2] for e in env.log if e[0] == "popup"], [full])

    def test_no_details_available_drops_popup_mention(self):
        agent, env, model = make([
            native_reply(("reply", {"speech": "It's the Curves tool. I've put the details in the popup."})),
            text_reply("It's the Curves tool."),
        ])
        run(self._go(agent, env, "which tool"))
        self.assertFalse([e for e in env.log if e[0] == "popup"])
        spoken = [e[1] for e in env.log if e[0] == "say"][-1]
        self.assertEqual(spoken, "It's the Curves tool.")

    def test_server_default_sampling_not_sent(self):
        agent, env, model = make([native_reply(("reply", {"speech": "hi"})), native_reply(("reply", {"speech": "hi"}))])
        run(self._go(agent, env, "hi"))
        self.assertNotIn("temperature", model.requests[0])
        self.assertNotIn("max_tokens", model.requests[0])
        agent.settings.update({"model": {"temperature": 0.2, "max_tokens": 1024}})
        run(self._go(agent, env, "again"))
        self.assertEqual(model.requests[1]["temperature"], 0.2)
        self.assertEqual(model.requests[1]["max_tokens"], 1024)

    async def _go(self, agent, env, text):
        await agent.run(text, await env.capture())


    def test_short_details_are_spoken_not_popped(self):
        agent, env, _ = make([native_reply(("reply", {"speech": "It's in your Downloads folder.",
                                                       "popup_markdown": "The file report.pdf is in your Downloads folder, saved yesterday."}))])
        run(self._go(agent, env, "where is the report"))
        self.assertFalse([e for e in env.log if e[0] == "popup"])
        spoken = [e[1] for e in env.log if e[0] == "say"][-1]
        self.assertIn("report.pdf", spoken)

    def test_question_does_not_open_popup(self):
        agent, env, _ = make([native_reply(("ask_user", {"question": "Which email do you mean, Priya's or Sam's?"}))])
        run(self._go(agent, env, "reply to that email"))
        self.assertFalse([e for e in env.log if e[0] == "popup"])
        self.assertTrue([e for e in env.log if e[0] == "say" and "Priya" in e[1]])

    def test_structured_details_do_open_popup(self):
        steps = "1. Open Settings\n2. Choose Display\n3. Turn on Night light"
        agent, env, _ = make([native_reply(("reply", {"speech": "Three quick steps, they're in the popup.", "popup_markdown": steps}))])
        run(self._go(agent, env, "how do I turn on night light"))
        self.assertEqual([e[2] for e in env.log if e[0] == "popup"], [steps])


class UnitTests(unittest.TestCase):
    def test_calibration_detects_conventions(self):
        m = CoordMapper(Path(os.environ["ATHENA_HOME"]) / "cal.json")
        w, h = 1344, 756
        truth = [(161, 121), (1156, 136), (672, 378), (242, 635), (1075, 605)]
        r = m.fit("pix", [(x + 3, y - 2) for x, y in truth], truth, w, h)
        self.assertEqual(r["convention"], "pixels")
        self.assertLess(r["error_px"], 2)
        r = m.fit("q3", [(x / w * 1000, y / h * 1000) for x, y in truth], truth, w, h)
        self.assertEqual(r["convention"], "norm1000")
        ix, iy = m.map("q3", 500, 500, w, h)
        self.assertAlmostEqual(ix, 672, delta=1)
        r = m.fit("n1", [(x / w, y / h) for x, y in truth], truth, w, h)
        self.assertEqual(r["convention"], "norm1")

    def test_parse_points_formats(self):
        self.assertEqual(parse_points({"a": [1, 2], "B": {"x": 3, "y": 4}}, "AB"), {"A": (1, 2), "B": (3, 4)})
        self.assertEqual(parse_points([{"label": "C", "bbox_2d": [0, 0, 10, 20]}], "C"), {"C": (5.0, 10.0)})

    def test_strip_think_variants(self):
        self.assertEqual(strip_think("<think>a\nb</think>\n\nHello"), "Hello")
        self.assertEqual(strip_think("<thinking>x</thinking>Hi"), "Hi")
        self.assertEqual(strip_think("pondering...</think>Answer"), "Answer")
        self.assertEqual(strip_think("Answer<think>never closed"), "Answer")
        self.assertEqual(strip_think("[THINK]hmm[/THINK]Yes"), "Yes")

    def test_hotkey_matcher(self):
        m = HotkeyMatcher("ctrl+win")
        self.assertEqual(m.key_down(0xA2), (None, False))
        self.assertEqual(m.key_down(0x5B), ("down", False))
        self.assertEqual(m.key_up(0x5B), ("up", True, True))   # swallow + mask Start menu
        m = HotkeyMatcher("ctrl+win")
        m.key_down(0xA2); m.key_down(0x5B)
        self.assertEqual(m.key_down(0x25)[0], "cancel")         # Ctrl+Win+Left is a Windows shortcut
        m = HotkeyMatcher("alt+space")
        m.key_down(0xA4)
        self.assertEqual(m.key_down(0x20), ("down", True))      # space swallowed

    def test_settings_masks_and_keeps_secret(self):
        s = Settings(Path(os.environ["ATHENA_HOME"]) / "sec.json")
        s.update({"model": {"providers": {"venice": {"api_key": "sk-secret-1234"}}}})
        pub = s.public()["model"]["providers"]["venice"]["api_key"]
        self.assertTrue(pub.startswith("•") and pub.endswith("1234"))
        s.update({"model": {"providers": {"venice": {"api_key": pub}}}})  # UI echoes the mask back
        self.assertEqual(s.get("model.providers.venice.api_key"), "sk-secret-1234")

    def test_speech_cleaning(self):
        from athena_core.voice import clean_for_speech, split_sentences
        self.assertEqual(clean_for_speech("<think>x</think>**Bold** and `code` [link](http://x)"), "Bold and code link")
        self.assertEqual(split_sentences("Hi there. How are you? Fine."), ["Hi there.", "How are you?", "Fine."])


if __name__ == "__main__":
    unittest.main()
