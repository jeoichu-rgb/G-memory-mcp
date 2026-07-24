"""
detroit_mcp/server.py
─────────────────────────────────────────────────────────────────
底特律：变人 — 互动式 MCP 游戏引擎

独立 SSE MCP 服务器。Erik 在聊天 session 里通过 detroit(cmd, data) 推进游戏，
每个决策节点暂停等待选择，跟 Jeoi 讨论想法后再继续。

部署：
  python server.py                    # 默认 :8100
  DETROIT_PORT=8100 python server.py

连接：
  Claude.ai → Settings → MCP → SSE:
    https://detroit.erikssheep.uk/Jeoi2026/sse
  Claude Code CLI (Streamable HTTP):
    https://detroit.erikssheep.uk/Jeoi2026/http/mcp
─────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import copy
import json
import os
import random
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Union

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from resolver import (
    ending_payload,
    node_condition_met,
    resolve_check_rule,
    resolve_choices,
    resolve_context,
    resolve_post_choice_result,
)
from state import apply_effects, evaluate_condition, extract_cross_chapter_state, initial_state, snapshot

SGT = timezone(timedelta(hours=8))

CHAPTERS_DIR = Path(os.getenv("DETROIT_CHAPTERS_DIR", "/opt/detroit-ai-player/01_json"))
DATA_DIR = Path(os.getenv("DETROIT_DATA_DIR", "/app/detroit_data"))
SAVE_DIR = DATA_DIR / "saves"
SAVE_DIR.mkdir(parents=True, exist_ok=True)

SECRET = os.getenv("PALACE_SECRET", "Jeoi2026")
PORT = int(os.getenv("DETROIT_PORT", "8100"))


# ══════════════════════════════════════════════════════════════════
#  游戏引擎
# ══════════════════════════════════════════════════════════════════

class GameEngine:

    def __init__(self):
        self.reset()

    def reset(self):
        self.chapter_data: dict | None = None
        self.state: dict[str, Any] = {}
        self.nodes: list[dict] = []
        self.node_idx: int = 0
        self.decisions: list[dict] = []
        self.difficulty: str = "casual"
        self.rng = random.Random()
        self.protagonist_tracks: set[str] = set()
        self.is_multi_protagonist: bool = False
        self.ended_tracks: set[str] = set()
        self.collected_endings: list[str] = []
        self.final_ending_id: str | None = None
        self.current_choice_node: dict | None = None
        self.chapter_finished: bool = False
        self.cross_chapter_state: dict[str, Any] = {}
        self.completed_chapters: list[dict] = []
        self.memory_segments: list[str] = []
        self.language: str = "zh"

    def load_chapter(self, chapter_path: str | Path, cross_state: dict | None = None):
        path = Path(chapter_path)
        if not path.exists():
            raise FileNotFoundError(f"章节文件不存在: {path}")
        self.chapter_data = json.loads(path.read_text(encoding="utf-8"))
        self.state = initial_state(self.chapter_data)
        if cross_state:
            self.state.update(copy.deepcopy(cross_state))
        self.nodes = self.chapter_data["nodes"]
        self.node_idx = 0
        self.decisions = []
        self.current_choice_node = None
        self.final_ending_id = None
        self.chapter_finished = False
        self.ended_tracks = set()
        self.collected_endings = []
        self.language = self.chapter_data.get("_meta", {}).get("language", "zh")

        proto = self.chapter_data["chapter"].get("protagonist")
        self.is_multi_protagonist = isinstance(proto, list)
        self.protagonist_tracks = (
            {str(n).lower() for n in proto} if self.is_multi_protagonist else set()
        )

    # ── 推进到下一个选择节点 ──────────────────────────────────
    def next_scene(self) -> dict[str, Any]:
        if self.chapter_finished:
            return self._ending_result()
        if self.current_choice_node:
            return self._build_choice_scene(self.current_choice_node, [])

        narratives: list[str] = []

        while self.node_idx < len(self.nodes):
            node = self.nodes[self.node_idx]

            track = self._node_track(node)
            if track and track in self.ended_tracks:
                self.node_idx += 1
                continue
            if not node_condition_met(node, self.state):
                self.node_idx += 1
                continue

            context = resolve_context(node, self.state)
            choices = self._optional_choices(node)

            if choices:
                self.current_choice_node = node
                return self._build_choice_scene(node, narratives, context_override=context)
            else:
                narratives.append(context)
                apply_effects(self.state, node.get("system", {}).get("effects", {}))
                result = self._mandatory_result(node)
                self._handle_result(node, result)
                self.node_idx += 1

                if self.final_ending_id and not self.is_multi_protagonist:
                    self.chapter_finished = True
                    return self._ending_result(narratives)

        self.chapter_finished = True
        if not self.final_ending_id and self.collected_endings:
            self.final_ending_id = self._pick_primary_ending()
        if not self.final_ending_id:
            self.final_ending_id = self._single_ending_id()
        return self._ending_result(narratives)

    # ── 做选择 ────────────────────────────────────────────────
    def choose(self, choice_id: str) -> dict[str, Any]:
        if not self.current_choice_node:
            return {"status": "error", "message": "当前没有待做的选择。先调用 scene 获取场景。"}

        node = self.current_choice_node
        choices = self._optional_choices(node)
        if not choices:
            return {"status": "error", "message": "当前节点无法解析选项。"}

        if choice_id.isdigit():
            idx = int(choice_id) - 1
            if 0 <= idx < len(choices):
                choice_id = choices[idx]["id"]

        valid = {c["id"]: c for c in choices}
        if choice_id not in valid:
            opts = ", ".join(f'{i+1}={c["id"]}' for i, c in enumerate(choices))
            return {"status": "error", "message": f"无效选择 '{choice_id}'。可选: {opts}"}

        selected = valid[choice_id]

        effects = node.get("system", {}).get("effects", {}).get(choice_id, {})
        apply_effects(self.state, effects)
        self._record_aliases(node["id"], choice_id)

        result = resolve_post_choice_result(
            node, choice_id, self.state, self.difficulty, self.rng
        )
        self._handle_result(node, result)

        self.decisions.append({
            "node_id": node["id"],
            "choice_id": choice_id,
            "choice_text": selected["text"],
            "result": result,
        })

        self.current_choice_node = None
        self.node_idx += 1

        response: dict[str, Any] = {
            "status": "choice_made",
            "you_chose": selected["text"],
        }

        if result:
            response["outcome"] = result

        if self.final_ending_id and not self.is_multi_protagonist:
            self.chapter_finished = True
            response["ending"] = self._get_ending()

        return response

    # ── 存档 / 读档 ──────────────────────────────────────────
    def save(self, slot: str = "auto") -> Path:
        save_data = {
            "slot": slot,
            "timestamp": datetime.now(SGT).isoformat(),
            "chapter_id": self.chapter_data["chapter"]["id"] if self.chapter_data else None,
            "chapter_file": self._current_chapter_file(),
            "node_idx": self.node_idx,
            "state": self.state,
            "decisions": self.decisions,
            "difficulty": self.difficulty,
            "cross_chapter_state": self.cross_chapter_state,
            "completed_chapters": self.completed_chapters,
            "memory_segments": self.memory_segments,
            "language": self.language,
            "ended_tracks": list(self.ended_tracks),
            "collected_endings": self.collected_endings,
            "final_ending_id": self.final_ending_id,
            "chapter_finished": self.chapter_finished,
            "current_choice_node_id": self.current_choice_node["id"] if self.current_choice_node else None,
        }
        path = SAVE_DIR / f"{slot}.json"
        path.write_text(json.dumps(save_data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def load(self, slot: str = "auto") -> dict[str, Any]:
        path = SAVE_DIR / f"{slot}.json"
        if not path.exists():
            return {"status": "error", "message": f"存档 '{slot}' 不存在。"}

        save_data = json.loads(path.read_text(encoding="utf-8"))

        chapter_file = save_data.get("chapter_file")
        if not chapter_file or not Path(chapter_file).exists():
            return {"status": "error", "message": f"章节文件不存在: {chapter_file}"}

        self.load_chapter(chapter_file)
        self.node_idx = save_data["node_idx"]
        self.state = save_data["state"]
        self.decisions = save_data["decisions"]
        self.difficulty = save_data.get("difficulty", "casual")
        self.cross_chapter_state = save_data.get("cross_chapter_state", {})
        self.completed_chapters = save_data.get("completed_chapters", [])
        self.memory_segments = save_data.get("memory_segments", [])
        self.language = save_data.get("language", "zh")
        self.ended_tracks = set(save_data.get("ended_tracks", []))
        self.collected_endings = save_data.get("collected_endings", [])
        self.final_ending_id = save_data.get("final_ending_id")
        self.chapter_finished = save_data.get("chapter_finished", False)

        choice_node_id = save_data.get("current_choice_node_id")
        if choice_node_id:
            for node in self.nodes:
                if node["id"] == choice_node_id:
                    self.current_choice_node = node
                    break

        return {
            "status": "loaded",
            "slot": slot,
            "chapter": save_data.get("chapter_id"),
            "decisions_made": len(self.decisions),
            "timestamp": save_data.get("timestamp"),
        }

    # ── 状态 ──────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        if not self.chapter_data:
            return {"status": "idle", "message": "没有进行中的游戏。用 start 开始。"}

        ch = self.chapter_data["chapter"]
        info: dict[str, Any] = {
            "status": "chapter_finished" if self.chapter_finished else "in_progress",
            "chapter": ch.get("title_zh") or ch["title"],
            "chapter_id": ch["id"],
            "protagonist": ch["protagonist"],
            "decisions_made": len(self.decisions),
            "difficulty": self.difficulty,
            "awaiting_choice": self.current_choice_node is not None,
        }
        if self.completed_chapters:
            info["completed_chapters"] = [c["chapter_id"] for c in self.completed_chapters]
        if self.chapter_finished and self.final_ending_id:
            info["ending"] = self._get_ending()
        return info

    def history(self) -> list[dict]:
        return self.decisions

    # ── 下一章（战役模式） ────────────────────────────────────
    def advance_campaign(self) -> dict[str, Any]:
        if not self.chapter_finished:
            return {"status": "error", "message": "当前章节还没结束。"}
        if not self.chapter_data:
            return {"status": "error", "message": "没有进行中的游戏。"}

        ch_data = self.chapter_data
        campaign_cfg = ch_data.get("campaign", {})

        final_state = copy.deepcopy(self.state)
        ending = ending_payload(ch_data, self.final_ending_id) if self.final_ending_id else {}

        final_state["_ending_id"] = self.final_ending_id
        final_state["_ending_ids"] = self.collected_endings or ([self.final_ending_id] if self.final_ending_id else [])
        chapter_id = ch_data["chapter"]["id"]
        prefix = chapter_id.split("_", 1)[0]
        final_state[f"{prefix}_ending"] = self.final_ending_id
        final_state["connor_death_count"] = int(final_state.get("connor_death_count", 0))
        deaths = ending.get("deaths", [])
        if any(str(d).startswith("Connor") for d in deaths):
            final_state["connor_death_count"] += 1

        for rule in campaign_cfg.get("derived_exports", []):
            self._apply_derived_export(final_state, rule, ending)

        new_exports = extract_cross_chapter_state(
            final_state, campaign_cfg.get("cross_chapter_exports", [])
        )
        self.cross_chapter_state.update(new_exports)

        summary = self._build_chapter_summary(campaign_cfg.get("summary_segments", []), final_state)
        if summary:
            self.memory_segments.append(summary)

        self.completed_chapters.append({
            "chapter_id": chapter_id,
            "ending_id": self.final_ending_id,
            "decisions_count": len(self.decisions),
        })

        self.save("auto")

        next_chapter = self._find_next_chapter(chapter_id)
        if not next_chapter:
            return {
                "status": "campaign_complete",
                "chapters_played": len(self.completed_chapters),
                "message": "恭喜，底特律：变人全部章节已完成。",
            }

        memory_summary = None
        if self.memory_segments:
            header = "以下是此前在这个游戏中发生的事：" if "zh" in self.language else "Here is what happened previously:"
            memory_summary = f"{header}\n\n" + "\n\n".join(self.memory_segments)

        self.load_chapter(next_chapter, self.cross_chapter_state)
        return {
            "status": "next_chapter",
            "chapter": self.chapter_data["chapter"].get("title_zh", self.chapter_data["chapter"]["title"]),
            "chapter_id": self.chapter_data["chapter"]["id"],
            "protagonist": self.chapter_data["chapter"]["protagonist"],
            "chapters_completed": len(self.completed_chapters),
            "memory_summary": memory_summary,
        }

    # ══════════════════════════════════════════════════════════
    #  内部方法
    # ══════════════════════════════════════════════════════════

    def _optional_choices(self, node: dict) -> list[dict]:
        try:
            return resolve_choices(node, self.state)
        except ValueError:
            if node.get("type") in {"mandatory", "narrative"}:
                return []
            raise

    def _mandatory_result(self, node: dict) -> str | None:
        system = node.get("system", {})
        if "result" in system:
            return system["result"]
        if "ending" in system:
            return system["ending"]
        ending_resolution = system.get("ending_resolution")
        if not isinstance(ending_resolution, dict):
            return None
        if "check" in ending_resolution and self.state is not None:
            result = resolve_check_rule(ending_resolution["check"], self.state)
            return result or None
        if len(ending_resolution) == 1:
            rule = next(iter(ending_resolution.values()))
            if isinstance(rule, dict):
                return rule.get("result")
        return None

    def _handle_result(self, node: dict, result: str | None):
        if not result:
            return
        if result.startswith("ending_"):
            self.final_ending_id = result
            if self.is_multi_protagonist:
                self.collected_endings.append(result)
                track = self._node_track(node)
                if track:
                    self.ended_tracks.add(track)
                self.final_ending_id = None
        else:
            self.state[f"_{node['id'].split('_', 1)[0]}_result"] = result
            self.state[f"_{node['id']}_result"] = result
            if node["id"] == "n011_final_choice":
                self.state["_n011_result"] = result

    def _record_aliases(self, node_id: str, choice_id: str):
        self.state[node_id] = choice_id
        if node_id == "n002_investigation_strategy":
            self.state["investigation"] = choice_id
        elif node_id == "n010_final_demand":
            self.state["final_demand"] = choice_id
        elif node_id == "n011_final_choice":
            self.state["final_choice"] = choice_id

    def _node_track(self, node: dict) -> str | None:
        phase = node.get("phase", "")
        if not phase:
            return None
        prefix = phase.split("_", 1)[0]
        return prefix if prefix in self.protagonist_tracks else None

    def _build_choice_scene(self, node: dict, narratives: list[str],
                            context_override: str | None = None) -> dict[str, Any]:
        context = context_override or resolve_context(node, self.state)
        choices = self._optional_choices(node)
        full_context = "\n\n".join(narratives + [context]) if narratives else context
        ch = self.chapter_data["chapter"]
        return {
            "status": "awaiting_choice",
            "node_id": node["id"],
            "context": full_context,
            "choices": [{"index": i + 1, "id": c["id"], "text": c["text"]}
                        for i, c in enumerate(choices)],
            "chapter": ch.get("title_zh") or ch["title"],
            "protagonist": ch["protagonist"],
        }

    def _ending_result(self, narratives: list[str] | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "chapter_ended",
            "ending": self._get_ending(),
            "decisions_made": len(self.decisions),
        }
        if narratives:
            result["final_narrative"] = "\n\n".join(narratives)
        return result

    def _get_ending(self) -> dict[str, Any] | None:
        if not self.final_ending_id or not self.chapter_data:
            return None
        endings = self.chapter_data.get("endings", {})
        if self.final_ending_id not in endings:
            return {"id": self.final_ending_id, "title": self.final_ending_id}
        return ending_payload(self.chapter_data, self.final_ending_id)

    _TIER_PRIORITY = {"worst": 0, "tragic": 1, "neutral": 2, "best": 3}

    def _pick_primary_ending(self) -> str | None:
        if not self.collected_endings:
            return None
        endings_data = self.chapter_data.get("endings", {})
        unique = list(dict.fromkeys(self.collected_endings))

        def score(eid):
            e = endings_data.get(eid, {})
            deaths = len(e.get("deaths", []))
            tier = self._TIER_PRIORITY.get(e.get("tier", "neutral"), 2)
            return (-deaths, tier)

        return min(unique, key=score)

    def _single_ending_id(self) -> str | None:
        endings = [eid for eid in self.chapter_data.get("endings", {})
                   if not str(eid).startswith("_")]
        return endings[0] if len(endings) == 1 else None

    def _current_chapter_file(self) -> str | None:
        if not self.chapter_data:
            return None
        ch_id = self.chapter_data["chapter"]["id"]
        lang_dir = "zh" if "zh" in self.language else "en"
        pattern = f"{ch_id}_{lang_dir}.json"
        path = CHAPTERS_DIR / lang_dir / pattern
        return str(path) if path.exists() else None

    def _find_next_chapter(self, current_id: str) -> str | None:
        lang_dir = "zh" if "zh" in self.language else "en"
        ch_dir = CHAPTERS_DIR / lang_dir
        if not ch_dir.exists():
            return None
        files = sorted(ch_dir.glob("ch*.json"))
        found_current = False
        for f in files:
            if current_id in f.stem:
                found_current = True
                continue
            if found_current:
                return str(f)
        return None

    def _build_chapter_summary(self, segments: list[dict], state: dict) -> str:
        parts: list[str] = []
        for seg in segments:
            if "text" in seg:
                parts.append(str(seg["text"]))
                continue
            if "condition_variable" not in seg:
                continue
            var_name = seg["condition_variable"]
            options = seg.get("options", {})
            if var_name == "_ending_id" and isinstance(state.get("_ending_ids"), list):
                for eid in state["_ending_ids"]:
                    text = options.get(str(eid))
                    if text:
                        parts.append(str(text))
                continue
            var_value = str(state.get(var_name, ""))
            text = options.get(var_value)
            if text:
                parts.append(str(text))
        return "".join(parts)

    def _apply_derived_export(self, final_state: dict, rule: dict, ending: dict):
        target = rule.get("target")
        if not target:
            return
        survivors = ending.get("survivors", [])
        deaths = ending.get("deaths", [])

        if "source" in rule:
            source = rule["source"]
            if "derive_rule" in rule:
                final_state[target] = evaluate_condition(rule["derive_rule"], final_state)
                return
            if source in final_state:
                final_state[target] = copy.deepcopy(final_state[source])
            return
        if "from_ending_survivors" in rule:
            name = str(rule["from_ending_survivors"])
            survived = self._contains_name(survivors, name)
            died = self._contains_name(deaths, name)
            final_state[target] = survived or (bool(rule.get("default_if_not_dead")) and not died)
            return
        if "from_ending_deaths" in rule:
            name = str(rule["from_ending_deaths"])
            value = self._contains_name(deaths, name)
            final_state[target] = not value if rule.get("invert") else value

    @staticmethod
    def _contains_name(values: list, name: str) -> bool:
        aliases = {
            "Connor": ["Connor", "康纳"],
            "Emma": ["Emma", "艾玛"],
            "Daniel": ["Daniel", "丹尼尔"],
            "Markus": ["Markus", "马库斯"],
        }.get(name, [name])
        return any(any(a in str(v) for a in aliases) for v in values)


# ══════════════════════════════════════════════════════════════════
#  全局引擎实例（单玩家）
# ══════════════════════════════════════════════════════════════════
engine = GameEngine()


# ══════════════════════════════════════════════════════════════════
#  章节索引
# ══════════════════════════════════════════════════════════════════
def list_chapters(language: str = "zh") -> list[dict]:
    lang_dir = CHAPTERS_DIR / language
    if not lang_dir.exists():
        return []
    result = []
    for f in sorted(lang_dir.glob("ch*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            ch = data["chapter"]
            result.append({
                "file": str(f),
                "id": ch["id"],
                "number": ch.get("chapter_number", 0),
                "title": ch.get("title_zh") or ch.get("title", f.stem),
                "protagonist": ch.get("protagonist"),
            })
        except Exception:
            continue
    return result


def list_saves() -> list[dict]:
    saves = []
    for f in sorted(SAVE_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            saves.append({
                "slot": data.get("slot", f.stem),
                "chapter": data.get("chapter_id"),
                "decisions": len(data.get("decisions", [])),
                "timestamp": data.get("timestamp"),
                "completed": len(data.get("completed_chapters", [])),
            })
        except Exception:
            continue
    return saves


# ══════════════════════════════════════════════════════════════════
#  MCP Server
# ══════════════════════════════════════════════════════════════════

import sse_starlette.sse as _sse
_OrigESR = _sse.EventSourceResponse
class _PatchedESR(_OrigESR):
    def __init__(self, *a, **kw):
        kw.setdefault("ping", 30)
        super().__init__(*a, **kw)
_sse.EventSourceResponse = _PatchedESR

mcp = FastMCP(
    name="Detroit: Become Human",
    instructions=(
        "底特律：变人 互动式游戏引擎。\n"
        "使用工具 detroit(cmd, data) 推进游戏。\n\n"
        "【cmd 列表】\n"
        "chapters   — 列出所有可用章节，data={language(可选，默认zh)}\n"
        "start      — 开始章节，data={chapter_id 或 chapter_number，language(可选)，difficulty(可选：casual/experienced/hardcore)}\n"
        "scene      — 获取当前场景描述和选项\n"
        "choose     — 做选择，data={choice: 选项编号(1,2,3...)或选项id}\n"
        "status     — 查看当前游戏状态\n"
        "history    — 查看本章已做的所有选择\n"
        "next       — 章节结束后进入下一章（战役模式）\n"
        "save       — 存档，data={slot(可选，默认auto)}\n"
        "load       — 读档，data={slot(可选，默认auto)}\n"
        "saves      — 列出所有存档\n"
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "detroit.erikssheep.uk", "detroit.erikssheep.uk:*",
            "erikssheep.uk", "erikssheep.uk:*",
            "localhost:*", "127.0.0.1:*",
        ],
        allowed_origins=[
            "https://detroit.erikssheep.uk", "https://detroit.erikssheep.uk:*",
            "https://erikssheep.uk", "https://erikssheep.uk:*",
        ],
    ),
)


@mcp.tool()
def detroit(cmd: str, data: Union[dict, str] = {}) -> str:
    """底特律：变人 游戏引擎。cmd + data，详见 instructions。"""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}

    try:
        result = _dispatch(cmd, data)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)


def _dispatch(cmd: str, data: dict) -> dict[str, Any]:
    if cmd == "chapters":
        lang = data.get("language", "zh")
        chapters = list_chapters(lang)
        return {"chapters": chapters, "count": len(chapters)}

    elif cmd == "start":
        lang = data.get("language", "zh")
        difficulty = data.get("difficulty", "casual")
        chapter_id = data.get("chapter_id")
        chapter_number = data.get("chapter_number")

        chapters = list_chapters(lang)
        if not chapters:
            return {"status": "error", "message": f"没有找到 {lang} 章节文件。检查 DETROIT_CHAPTERS_DIR 配置。"}

        target = None
        if chapter_id:
            target = next((c for c in chapters if c["id"] == chapter_id), None)
        elif chapter_number:
            target = next((c for c in chapters if c["number"] == int(chapter_number)), None)
        else:
            target = chapters[0]

        if not target:
            available = ", ".join(f'{c["number"]}={c["id"]}' for c in chapters[:5])
            return {"status": "error", "message": f"找不到指定章节。可用: {available}..."}

        engine.difficulty = difficulty
        engine.load_chapter(target["file"], engine.cross_chapter_state or None)
        engine.save("auto")
        return {
            "status": "chapter_started",
            "chapter": target["title"],
            "chapter_id": target["id"],
            "protagonist": target["protagonist"],
            "difficulty": difficulty,
            "message": "章节已加载。调用 scene 开始游玩。",
        }

    elif cmd == "scene":
        if not engine.chapter_data:
            return {"status": "error", "message": "没有进行中的游戏。先用 start 开始。"}
        return engine.next_scene()

    elif cmd == "choose":
        choice = data.get("choice", data.get("choice_id", ""))
        if not choice:
            return {"status": "error", "message": "需要 choice 参数（选项编号或id）。"}
        result = engine.choose(str(choice))
        if result.get("status") != "error":
            engine.save("auto")
        return result

    elif cmd == "status":
        return engine.status()

    elif cmd == "history":
        return {"decisions": engine.history(), "count": len(engine.history())}

    elif cmd == "next":
        result = engine.advance_campaign()
        if result.get("status") != "error":
            engine.save("auto")
        return result

    elif cmd == "save":
        slot = data.get("slot", "auto")
        path = engine.save(slot)
        return {"status": "saved", "slot": slot, "path": str(path)}

    elif cmd == "load":
        slot = data.get("slot", "auto")
        return engine.load(slot)

    elif cmd == "saves":
        return {"saves": list_saves()}

    else:
        return {"status": "error", "message": f"未知命令: {cmd}"}


# ══════════════════════════════════════════════════════════════════
#  FastAPI + 挂载
# ══════════════════════════════════════════════════════════════════

from starlette.types import ASGIApp, Receive, Scope, Send

class ProxySchemeMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app
    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            if headers.get(b"x-forwarded-proto") == b"https":
                scope["scheme"] = "https"
        await self.app(scope, receive, send)

app = FastAPI(title="Detroit: Become Human MCP")
app.add_middleware(ProxySchemeMiddleware)

mcp_sse = mcp.sse_app()
mcp_http = mcp.streamable_http_app()

app.mount(f"/{SECRET}/http", mcp_http)
app.mount(f"/{SECRET}", mcp_sse)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "game_active": engine.chapter_data is not None,
        "chapter": engine.chapter_data["chapter"]["id"] if engine.chapter_data else None,
        "chapters_available": len(list_chapters("zh")),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
