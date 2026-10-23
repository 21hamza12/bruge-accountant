"""The in-app chat, part 1: plain code, no AI (docs/design/07-chat.md, 12-chat-part1.md).

* ``text``: reads French free text (accents and case ignored): dates, tickets, signs, amounts.
* ``reply``: the ``Reply`` a lookup returns, and its stored and rendered forms.
* ``tools``: the registry of fixed, read-only lookups (name, description, permission, typed
  arguments, handler). Part 2 exposes the same registry to Claude.
* ``router``: ``parse_message`` (text to a tool call, pure) and ``route_message`` (the one entry
  point, with the ``unknown`` seam part 2 plugs the AI into).
* ``store``: the append-only conversation of one person.

Nothing here writes except ``store``; nothing here reads the agency from the message.
"""
