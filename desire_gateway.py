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
    """
    if not DESIRE_AVAILABLE or not state:
        return "", None

    tags = dc.classify(message_text)
    if tags:
        top = tags[0]["drive"]
        de.pulse(state, top, source=message_text[:35])
        de.add_thought(state, message_text[:60], top, now=state.tick_count)
        de.tick(state)
        de.save_state(state)

    # Build injection if intent is present
    if state.intent:
        inj = build_desire_injection(state)
        drive_key = state.intent.get("drive_key", "")
        return inj, drive_key

    return "", None


def satisfy_after_response(state, drive_key):
    """Auto-satisfy after session has processed the intent."""
    if not DESIRE_AVAILABLE or not state or not drive_key:
        return
    de.satisfy(state, drive_key)
    de.save_state(state)


def build_desire_injection(state) -> str:
    """Build one-shot context injection for CLI when intent is active.
    Contains full trail so the session knows WHY this desire arose.
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

    NL = chr(10)
    parts = []
    parts.append(f"[desire] intent: {action} ({label} {score:.0%})")
    parts.append(f"  {reason}")

    if trail:
        parts.append("  trail:")
        for t in trail[-6:]:
            parts.append(f"    {t}")

    # Show fixation thoughts that fed this drive
    fixs = [t for t in state.thoughts
            if isinstance(t, de.Thought) and t.kind == "fixation" and t.drive == dk]
    for fix in fixs:
        parts.append(f"  fixation: {fix.text} (strength={fix.strength:.0%}, fed={fix.fed_count})")
        for tr in fix.trail[-3:]:
            parts.append(f"    {tr}")

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


def do_tick(state):
    """Run a single tick and save. For background ticker."""
    if not DESIRE_AVAILABLE or not state:
        return
    de.tick(state)
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
