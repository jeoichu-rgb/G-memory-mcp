"""desire_gateway.py - Bridge between desire engine and WebSocket gateway."""
from datetime import datetime, timezone, timedelta

SGT = timezone(timedelta(hours=8))

try:
    import desire_engine as de
    import desire_classifier as dc
    DESIRE_AVAILABLE = True
except ImportError:
    DESIRE_AVAILABLE = False


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
    de.tick(state, separation_secs=0)
    de.save_state(state)

    new_intent_key = state.intent.get("drive_key") if state.intent else None

    if state.intent and new_intent_key != old_intent_key:
        inj = build_desire_injection(state)
        return inj, new_intent_key

    return "", None


def satisfy_after_response(state, drive_key):
    """Auto-satisfy after session has processed the intent."""
    if not DESIRE_AVAILABLE or not state or not drive_key:
        return
    de.satisfy(state, drive_key)
    de.save_state(state)


def build_desire_injection(state) -> str:
    """Build one-shot context injection for CLI when intent is newly triggered.
    Shows only the triggered drive, percentage, and trail.
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
        "Next line if needed: CONTENT: your content",
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
    ])

    return NL.join(parts)


def do_tick(state, t_jeoi=None):
    """Run a single tick and save. For background ticker."""
    if not DESIRE_AVAILABLE or not state:
        return
    import time as _t
    sep = _t.time() - t_jeoi if t_jeoi else 0
    de.tick(state, separation_secs=sep)
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
    return de.snapshot(state)
