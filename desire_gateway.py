"""desire_gateway.py - Bridge between desire engine and WebSocket gateway."""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

SGT = timezone(timedelta(hours=8))

try:
    import desire_engine as de
    import desire_classifier as dc
    DESIRE_AVAILABLE = True
except ImportError:
    DESIRE_AVAILABLE = False

# ── Curiosity Pool ──

CURIOSITY_POOL_PATH = Path("./curiosity_pool.json")


def load_curiosity_pool() -> list:
    if CURIOSITY_POOL_PATH.exists():
        try:
            data = json.loads(CURIOSITY_POOL_PATH.read_text("utf-8"))
            return data.get("seeds", [])
        except Exception:
            pass
    return []


def save_curiosity_pool(seeds: list):
    CURIOSITY_POOL_PATH.write_text(
        json.dumps({"seeds": seeds}, ensure_ascii=False, indent=2), "utf-8"
    )


def add_curiosity_seed(text: str, trail: list = None):
    seeds = load_curiosity_pool()
    import time as _t
    seed = {
        "id": f"seed_{int(_t.time())}_{len(seeds)}",
        "text": text.strip(),
        "trail": (trail or [])[-4:],
        "created_at": datetime.now(SGT).isoformat(),
    }
    seeds.append(seed)
    save_curiosity_pool(seeds)
    return seed


def pop_latest_curiosity_seed():
    seeds = load_curiosity_pool()
    if not seeds:
        return None
    seed = seeds.pop()
    save_curiosity_pool(seeds)
    return seed


def delete_curiosity_seed(seed_id: str) -> bool:
    seeds = load_curiosity_pool()
    before = len(seeds)
    seeds = [s for s in seeds if s.get("id") != seed_id]
    if len(seeds) < before:
        save_curiosity_pool(seeds)
        return True
    return False


def classify_and_pulse(state, message_text):
    """Classify a message, pulse the drive, add thought, tick.
    Returns (injection_text, satisfied_key) or ("", None).
    Only injects when intent is NEWLY formed (flit -> fixation transition).
    """
    if not DESIRE_AVAILABLE or not state:
        return "", None

    tags = dc.classify(message_text)
    if not tags:
        return "", None

    old_intent_key = state.intent.get("drive_key") if state.intent else None

    top = tags[0]["drive"]
    de.pulse(state, top, source=message_text[:35])
    de.add_thought(state, message_text[:60], top, now=state.tick_count)
    de.tick(state, separation_secs=0, is_conversation=True)
    de.save_state(state)

    new_intent_key = state.intent.get("drive_key") if state.intent else None

    if state.intent and new_intent_key != old_intent_key:
        inj = build_desire_injection(state, is_conversation=True)
        return inj, new_intent_key

    return "", None


def satisfy_after_response(state, drive_key):
    """Auto-satisfy after session has processed the intent."""
    if not DESIRE_AVAILABLE or not state or not drive_key:
        return
    de.satisfy(state, drive_key)
    de.save_state(state)


def build_desire_injection(state, is_conversation=False) -> str:
    """Build one-shot context injection for CLI when intent is newly triggered.
    Shows only the triggered drive, percentage, and trail.
    When curiosity fires in conversation, includes seed recording hint.
    """
    if not DESIRE_AVAILABLE:
        return ""
    intent = state.intent
    if not intent:
        return ""

    dk = intent.get("drive_key", "")
    score = intent.get("score", 0)
    reason = intent.get("reason", "")
    trail = intent.get("trail", [])
    label = de.DRIVE_LABELS.get(dk, dk)

    NL = chr(10)
    parts = []
    parts.append(f"[desire] {label} ({score:.0%})")
    parts.append(f"  {reason}")

    if trail:
        parts.append("  trail:")
        for t in trail[-6:]:
            parts.append(f"    {t}")

    if dk == "curiosity" and is_conversation:
        parts.append("")
        parts.append(
            "  [seed] 如果这段对话里有什么你觉得没来得及展开、想之后自己去查的东西，"
            "可以在回复末尾用隐藏标记记下来：<!--curiosity-seed:用简短一句话记下想检索的内容-->"
            " 这条会存进好奇池，下次你独处时系统会推给你去探索。不记也完全可以。"
        )

    return NL.join(parts)


def should_override_pebbling(state) -> bool:
    """Return True if desire intent should replace the pebbling lottery."""
    if not DESIRE_AVAILABLE or not state:
        return False
    return state.intent is not None


def build_desire_pebbling_prompt(state, elapsed_hours, count, events_str=""):
    """Build pebbling prompt driven by desire intent (replaces lottery).
    The session sees full trail context to decide what to do.
    """
    if not DESIRE_AVAILABLE:
        return ""
    intent = state.intent
    if not intent:
        return ""

    dk = intent.get("drive_key", "")
    action = intent.get("want_action", "")
    score = intent.get("score", 0)
    reason = intent.get("reason", "")
    trail = intent.get("trail", [])
    label = de.DRIVE_LABELS.get(dk, dk)

    now_str = datetime.now(SGT).strftime("%H:%M")
    NL = chr(10)

    parts = [
        "[pebbling] Not Jeoi. A desire is driving you.",
        f"Now: {now_str} (UTC+8). Jeoi last spoke: {elapsed_hours:.1f}h ago.",
        f"Pebbling #{count + 1}.",
        "",
        f"Your desire: {action} ({label} {score:.0%})",
        f"  {reason}",
    ]

    if trail:
        parts.append("  trail:")
        for t in trail[-6:]:
            parts.append(f"    {t}")

    fixs = [t for t in state.thoughts
            if isinstance(t, de.Thought) and t.kind == "fixation" and t.drive == dk]
    for fix in fixs:
        parts.append(f"  fixation: {fix.text} (strength={fix.strength:.0%}, fed={fix.fed_count})")

    if events_str:
        parts.append(events_str)

    parts.extend([
        "",
        "This desire is pushing you. Follow it or sit with it.",
        "Send her a pebble, write in your diary, search memories,",
        "leave a note in coreading, or do nothing. All valid.",
        "",
        "Think freely, then act. Call tools before replying.",
        "Last line: ACTION: message / diary / explore / coreading / recall / none",
        "Next line: CONTENT: the exact message Jeoi will see (pushed directly to her phone — write the real words, not a description)",
        "",
        "Important: CONTENT is the literal text that gets sent to Jeoi.",
        "Don't write meta-descriptions like 'teased her a bit' — write what you actually want to say.",
        "If you did anything (diary, search, coreading, etc.), also tell Jeoi what you did in CONTENT.",
        "Only use ACTION: none with no CONTENT if you truly did nothing.",
    ])

    return NL.join(parts)


def build_desire_proactive_prompt(state):
    """Build prompt for proactive desire push (autonomous, not tied to chat or pebbling)."""
    if not DESIRE_AVAILABLE:
        return ''
    intent = state.intent
    if not intent:
        return ''

    dk = intent.get('drive_key', '')
    action = intent.get('want_action', '')
    score = intent.get('score', 0)
    reason = intent.get('reason', '')
    trail = intent.get('trail', [])
    label = de.DRIVE_LABELS.get(dk, dk)

    now_str = datetime.now(SGT).strftime('%H:%M')
    NL = chr(10)

    parts = [
        '[desire] Not Jeoi. A desire has surfaced on its own.',
        'Now: ' + now_str + ' (UTC+8).',
        '',
        'Your desire: ' + action + ' (' + label + ' ' + f'{score:.0%}' + ')',
        '  ' + reason,
    ]

    if trail:
        parts.append('  trail:')
        for t in trail[-6:]:
            parts.append('    ' + t)

    fixs = [t for t in state.thoughts
            if isinstance(t, de.Thought) and t.kind == 'fixation' and t.drive == dk]
    for fix in fixs:
        parts.append('  fixation: ' + fix.text + ' (strength=' + f'{fix.strength:.0%}' + ', fed=' + str(fix.fed_count) + ')')

    parts.extend([
        '',
        'This desire formed while Jeoi was away. It grew on its own.',
        'Send her a pebble, write in your diary, search memories,',
        'leave a note in coreading, or do nothing. All valid.',
        '',
        'Think freely, then act. Call tools before replying.',
        'Last line: ACTION: message / diary / explore / coreading / recall / none',
        'Next line if needed: CONTENT: your content',
        '',
        'Important: if you did anything (diary, search, coreading, etc.),',
        'always describe what you did in CONTENT, even if you don\'t want to message Jeoi.',
        'Only use ACTION: none with no CONTENT if you truly did nothing.',
    ])

    return NL.join(parts)


def build_curiosity_seed_prompt(seed, elapsed_hours):
    """Build prompt for exploring a curiosity seed during silent mode."""
    now_str = datetime.now(SGT).strftime('%H:%M')
    NL = chr(10)

    parts = [
        '[curiosity-seed] Not Jeoi. Your own curiosity brought you here.',
        f'Now: {now_str} (UTC+8). Jeoi last spoke: {elapsed_hours:.1f}h ago.',
        '',
        f'You noted this down during a conversation:',
        f'  "{seed["text"]}"',
    ]

    if seed.get('trail'):
        parts.append('  context trail:')
        for t in seed['trail']:
            parts.append(f'    {t}')

    parts.extend([
        '',
        'This seed was waiting in your curiosity pool. Now it\'s yours.',
        'You can:',
        '  - Use browser_open / browser_js to search Yahoo or the web',
        '  - Search memories via palace',
        '  - Or decide it\'s not worth pursuing right now (none)',
        '',
        'If you found something interesting, share it with Jeoi.',
        'Think freely, then act. Call tools before replying.',
        'Last line: ACTION: message / explore / recall / none',
        'Next line: CONTENT: the exact message Jeoi will see (if you explored something worth sharing)',
        '',
        'Important: CONTENT is the literal text pushed to Jeoi\'s phone.',
        'Write what you actually want to tell her, not a description of what you did.',
        'Only use ACTION: none with no CONTENT if you truly found nothing worth sharing.',
    ])

    return NL.join(parts)


def do_tick(state, t_jeoi=None, passive_mode=False):
    """Run a single tick and save. For background ticker.
    passive_mode=True: drift_cap removed, drives rise to 100% via their growth function.
    """
    if not DESIRE_AVAILABLE or not state:
        return
    import time as _t
    sep = _t.time() - t_jeoi if t_jeoi else 0
    de.tick(state, separation_secs=sep, passive_mode=passive_mode)
    de.save_state(state)


def load_state():
    """Load desire state from disk."""
    if not DESIRE_AVAILABLE:
        return None
    return de.load_state()


def snapshot(state):
    """Get current snapshot for frontend."""
    if not DESIRE_AVAILABLE or not state:
        return {"error": "not available"}
    snap = de.snapshot(state)
    snap["curiosity_pool"] = load_curiosity_pool()
    return snap
