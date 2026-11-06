"""The literal text of the user-role rows the agent injects as scaffolding.

Dependency-free leaf shared by the turn loop (``agent.conversation_loop`` appends these
rows) and the compressor (``agent.context_compressor`` recognizes them BY CONTENT, so a
persisted nudge is not mistaken for a human turn). The compressor sits BELOW the turn
loop -- the database write path reaches it through ``hermes_state_messages`` -- and a
module-scope import of ``agent.conversation_loop`` from the compressor is a cycle (the
turn loop imports ``agent.turn_finalizer``, which imports the compressor). Keeping the
literals here, in a module that imports nothing, lets both layers depend on them with no
cycle and no import-time file I/O.

Keep the wording byte-identical to what earlier releases persisted: a nudge is matched by
content alone, so reworded text silently stops recognizing rows written by an older
version.
"""

from __future__ import annotations

# Named so _is_synthetic_compression_user_turn can recognize a crash-persisted nudge by
# content (SessionDB projection strips the _length_continuation_nudge tag).
_LENGTH_CONTINUATION_NETWORK_STUB = (
    "[System: The previous response was cut off by a network error mid-stream — a transport "
    "interruption, NOT a change in your capabilities. Your tools are still fully available; call "
    "them as normal and ignore any earlier claim that you lack tool access. Continue the task "
    "from where you left off. Do not restart or repeat prior text.]"
)
_LENGTH_CONTINUATION_OUTPUT_LIMIT = (
    "[System: Your previous response was truncated by the output length limit. Continue exactly "
    "where you left off. Do not restart or repeat prior text. Finish the answer directly.]"
)
# Pre-#74990 wording; kept so crash-persisted nudges from older sessions are still recognized.
_LEGACY_LENGTH_CONTINUATION_NETWORK_STUB = (
    "[System: The previous response was cut off by a network error mid-stream. Continue exactly "
    "where you left off. Do not restart or repeat prior text. Finish the answer directly.]"
)
# The dropped-tools variant interpolates tool names; matched by prefix.
_LENGTH_CONTINUATION_DROPPED_TOOLS_PREFIX = "[System: Your previous tool call "

# Codex/Responses turns that returned only internal reasoning: a bare retry would be
# byte-identical, so the model repeats it.
_CODEX_INCOMPLETE_NUDGE = (
    "[System: Your previous response contained only internal reasoning and never produced a "
    "visible answer or tool call. Do not keep thinking. Produce your final answer as plain text "
    "now (or make the tool call you were planning).]"
)


# Re-prompt after an acknowledgment-only Codex/Responses reply.
_CODEX_ACK_CONTINUATION_NUDGE = (
    "[System: Continue now. Execute the required tool calls and only send your final answer "
    "after completing the task.]"
)

# Re-prompt after a collapsed fragment ended a turn that had done real tool work (#103483). Asks
# for the same answer again when it WAS complete, so a false positive costs one call, never the answer.
_DEGENERATE_FINAL_NUDGE = (
    "[System: Your previous message ended the turn with a fragment that is not a usable answer. "
    "If the task is unfinished, continue it and then give the complete answer. If that fragment "
    "WAS your complete answer, send it again exactly as before.]"
)

# Re-prompt for finish_reason="tool_calls" with empty tool_calls (an interrupt mid-retry can persist it).
_DROPPED_TOOLCALL_NUDGE_CONTENT = (
    "Your previous turn indicated a tool call but none was included. Do not narrate a plan or "
    "restate intent — issue the actual tool call now to continue the task."
)

# Re-prompt for an empty response after tool calls (#9400); the metadata flag does not
# survive SessionDB projection, so it is matched by content.
_EMPTY_TOOL_RESPONSE_NUDGE = (
    "You just executed tool calls but returned an empty response. Please process the tool "
    "results above and continue with the task."
)

# The exact text of every user-role row the turn loop injects as scaffolding. A
# persisted nudge is recognized by content alone -- SessionDB drops the metadata tag
# that marked it -- so this set is the authoritative list of them. The dropped-tools
# variant interpolates tool names and is matched by PREFIX instead, so it is not here.
SYNTHETIC_NUDGE_TEXTS = frozenset({
    _CODEX_ACK_CONTINUATION_NUDGE, _CODEX_INCOMPLETE_NUDGE, _DEGENERATE_FINAL_NUDGE,
    _DROPPED_TOOLCALL_NUDGE_CONTENT, _EMPTY_TOOL_RESPONSE_NUDGE,
    _LEGACY_LENGTH_CONTINUATION_NETWORK_STUB, _LENGTH_CONTINUATION_NETWORK_STUB,
    _LENGTH_CONTINUATION_OUTPUT_LIMIT,
})
