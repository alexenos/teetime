"""The observer job: an independent reader of the tee sheet across the window.

Phase 0 of ``docs/design-observer-and-fanout.md`` (issue #189). Four
consecutive Fridays the requested slot was lost, and two incompatible
explanations fit every artifact we hold - a late gate, or a faster rival. They
cannot be separated from our own Reserve exchanges, because a refusal's body is
a re-render of our own pre-window snapshot (§7d of the post-mortem skill): the
verdict is live, the body is not. A reader that is not racing can photograph the
club's sheet during the window, and that separates them.

**This package never sends a Reserve.** That is a property of its import graph,
not a rule it follows: nothing here imports ``app.providers.walden_provider`` or
``app.providers.walden_http_booker``, which are the only two modules in the
project that know how to reserve a tee time. ``tests/test_observer.py`` asserts
it, so the guarantee fails loudly if a future import quietly reintroduces one.
"""
