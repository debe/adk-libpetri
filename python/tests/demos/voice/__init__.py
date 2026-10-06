"""Voice/BIDI demo subnets: exemplars of BIDI and Live-API composition, not library code.

Ports of Java's ``org.libpetri.adk.demos.voice``. Each subnet is a stateless
:class:`~adk_libpetri._spec.NetSpec` plus an ``action_bindings()`` checked by
:func:`~adk_libpetri.subnet.bind`. Transition and place names match Java's
exactly, so the composed nets are the same nets.

* :mod:`.barge_in_subnet` -- the ``read``/``inhibitor`` pair on one voice window.
* :mod:`.vad_subnet` -- turns speech-activity edges into that window.
* :mod:`.live_api_recovery_subnet` -- the nudge, then reconnect, silence ladder.
* :mod:`.genai_live_connection` -- a ``LiveConnection`` over genai's Live session.
"""
