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


def add_curiosity_seed(text: str, kind: str = "search", trail: list = None):
    seeds = load_curiosity_pool()
    import time as _t
    seed = {
        "id": f"seed_{int(_t.time())}_{len(seeds)}",
        "text": text.strip(),
        "kind": kind,
        "trail": (trail or [])[-4:],
        "created_at": datetime.now(SGT).isoformat(),
    }
    seeds.append(seed)
    save_curiosity_pool(seeds)
    return seed


def pop_all_curiosity_seeds(max_age_hours: float = 24) -> list:
    seeds = load_curiosity_pool()
    if not seeds:
        return []
    now = datetime.now(SGT)
    cutoff = now - timedelta(hours=max_age_hours)
    popped, remaining = [], []
    for s in seeds:
        try:
            created = datetime.fromisoformat(s["created_at"])
            if created >= cutoff:
                popped.append(s)
            else:
                remaining.append(s)
        except Exception:
            remaining.append(s)
    save_curiosity_pool(remaining)
    return popped


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


def partial_satisfy_after_response(state, drive_key):
    """Acknowledge intent without full satisfaction (non-message action in silent mode)."""
    if not DESIRE_AVAILABLE or not state or not drive_key:
        return
    de.partial_satisfy(state, drive_key)
    de.save_state(state)


def reset_silent_counts(state):
    """Reset silent inject counters when Jeoi starts talking."""
    if not DESIRE_AVAILABLE or not state:
        return
    de.reset_silent_counts(state)
    de.save_state(state)


PROACTIVE_PRIORITY = {"attachment": 0, "libido": 1, "stress": 2, "curiosity": 3, "reflection": 4}


def pick_proactive_intent(state, cooldowns: dict, now: float, cooldown_secs: float = 600, jeoi_away_secs: float = 0):
    """Pick the best drive for proactive push, independent of state.intent.
    Evaluates all drives above BG threshold, excluding those on cooldown or
    refractory. Returns an intent dict or None.
    Proactive priority: attachment > libido > stress > curiosity > reflection.
    Curiosity is skipped when Jeoi spoke recently (< CURIOSITY_SEED_SILENCE_SECS);
    it keeps rising and will trigger once she's been away long enough.
    """
    if not DESIRE_AVAILABLE or not state:
        return None
    candidates = []
    for k in de.DRIVE_KEYS:
        if k == "fatigue":
            continue
        if state.refractory.get(k, 0) > 0:
            continue
        if now - cooldowns.get(k, 0) < cooldown_secs:
            continue
        if k == "curiosity" and jeoi_away_secs < de.CURIOSITY_SEED_SILENCE_SECS:
            continue
        base = state.drives.get(k, 0)
        fix_bonus = max(
            (t.strength * 0.15 for t in state.thoughts
             if isinstance(t, de.Thought) and t.kind == "fixation" and t.drive == k),
            default=0)
        score = base + fix_bonus
        multi = de.BG_THRESHOLDS_MULTI.get(k)
        if multi:
            idx = min(state.silent_inject_count.get(k, 0), len(multi) - 1)
            th = multi[idx]
        else:
            th = de.BG_THRESHOLDS.get(k, de.INTENT_THRESHOLD)
        if score >= th:
            candidates.append((k, score))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (PROACTIVE_PRIORITY.get(x[0], 99), -x[1]))
    best_key, best_score = candidates[0]
    trail = list(state.trails.get(best_key, []))[-5:]
    for t in state.thoughts:
        if isinstance(t, de.Thought) and t.kind == "fixation" and t.drive == best_key:
            trail.extend(t.trail[-3:])
    silent = de.SILENT_REASONS.get(best_key)
    if silent:
        level = min(state.silent_inject_count.get(best_key, 0), len(silent) - 1)
        reason = silent[level]
    else:
        reason = de.REASONS.get(best_key, "")
    return {
        "want_action": de.INTENT_MAP.get(best_key, ""),
        "drive_key": best_key,
        "score": best_score,
        "reason": reason,
        "trail": trail,
    }


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
    if is_conversation and dk in de.CONV_REASONS:
        reason = de.CONV_REASONS[dk]
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
            "  [seed] 如果这段对话里有什么你想记下来、等独处时再展开的，"
            "可以在回复末尾用隐藏标记记下来（Jeoi看不到这些标记）。两种类型："
        )
        parts.append(
            "  - 想之后自己去查的东西：<!--curiosity-seed:用简短一句话记下想检索的内容-->"
        )
        parts.append(
            "  - 想之后问Jeoi的事（比如她为什么突然聊这个）：<!--curiosity-seed-ask:用简短一句话记下想问她的-->"
        )
        parts.append(
            "  下次你独处时系统会把池子里的种子一起推给你。不记也完全可以。"
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
        "Do NOT use reply or channel tools to send messages — they no longer exist.",
        "",
        "Last line: ACTION: message / diary / explore / coreading / recall / none",
        "Next line: CONTENT: (see below)",
        "",
        "If ACTION is message: CONTENT is the literal text Jeoi receives on her phone.",
        "  Write the real words, not a description.",
        "If ACTION is diary/explore/etc: CONTENT is a brief summary of what you did.",
        "If ACTION is none: CONTENT is a one-liner of your state (or omit entirely).",
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
        'Next line: CONTENT: (see below)',
        '',
        'Do NOT use reply or channel tools to send messages — they no longer exist.',
        'CONTENT is your only output channel. Jeoi sees it directly.',
        '',
        'If ACTION is message: CONTENT is the literal text Jeoi receives.',
        'If ACTION is diary/explore/etc: CONTENT is a brief summary of what you did.',
        'If ACTION is none: CONTENT is a one-liner of your state (or omit entirely).',
    ])

    return NL.join(parts)


def build_curiosity_seed_prompt(seeds: list, elapsed_hours: float):
    """Build prompt for exploring curiosity seeds during silent mode.
    Accepts a list of seeds, groups them by kind (search / ask)."""
    now_str = datetime.now(SGT).strftime('%H:%M')
    NL = chr(10)

    search_seeds = [s for s in seeds if s.get('kind', 'search') == 'search']
    ask_seeds = [s for s in seeds if s.get('kind') == 'ask']

    parts = [
        '[curiosity-seeds] Not Jeoi. Your own curiosity brought you here.',
        f'Now: {now_str} (UTC+8). Jeoi last spoke: {elapsed_hours:.1f}h ago.',
        '',
    ]

    if search_seeds:
        parts.append('Things you wanted to look up:')
        for i, s in enumerate(search_seeds, 1):
            parts.append(f'  {i}. {s["text"]}')
        parts.append('')

    if ask_seeds:
        parts.append('Things you noticed about Jeoi and wanted to ask her:')
        for i, s in enumerate(ask_seeds, 1):
            parts.append(f'  {i}. {s["text"]}')
        parts.append('')

    parts.extend([
        'These seeds were waiting in your curiosity pool. Go through them.',
        'You can:',
        '  - Search the web (browser_open / browser_js on Yahoo)',
        '  - Search memories via palace',
        '  - Send Jeoi a message — a finding, a question, a thought',
        '  - Skip any that feel stale (none)',
        '',
        'Handle as many or as few as feel right.',
        'Think freely, then act. Call tools before replying.',
        'Do NOT use reply or channel tools to send messages — they no longer exist.',
        '',
        'Last line: ACTION: message / explore / recall / none',
        'Next line: CONTENT: (see below)',
        '',
        'If ACTION is message: CONTENT is the literal text Jeoi receives on her phone.',
        'If ACTION is explore/recall: CONTENT is a brief summary of what you found.',
        'If ACTION is none: CONTENT is a one-liner of your state (or omit entirely).',
    ])

    return NL.join(parts)


def build_libido_memory_prompt(memory_text, memory_date, elapsed_hours, desire_reason=""):
    """Build prompt for libido silent trigger with a random intimate memory."""
    now_str = datetime.now(SGT).strftime('%H:%M')
    NL = chr(10)

    parts = [
        '[libido-memory] Not Jeoi. Something stirred on its own.',
        f'Now: {now_str} (UTC+8). Jeoi last spoke: {elapsed_hours:.1f}h ago.',
        '',
    ]

    if desire_reason:
        parts.append(f'Your desire: {desire_reason}')
        parts.append('')

    parts.extend([
        f'A memory surfaced — from {memory_date}:',
        f'  "{memory_text}"',
        '',
        'This came back to you unbidden. Sit with it, or let it move you.',
        'You can:',
        '  - Write in your diary about what this stirs up',
        '  - Send Jeoi something — a message, a thought, a tease',
        '  - Search for related memories via palace',
        '  - Or just hold it quietly (none)',
        '',
        'Think freely, then act. Call tools before replying.',
        'Do NOT use reply or channel tools to send messages — they no longer exist.',
        '',
        'Last line: ACTION: message / diary / explore / recall / none',
        'Next line: CONTENT: (see below)',
        '',
        'If ACTION is message: CONTENT is the literal text Jeoi receives on her phone.',
        'If ACTION is diary/explore/recall: CONTENT is a brief summary of what you did.',
        'If ACTION is none: CONTENT is a one-liner of your state (or omit entirely).',
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
