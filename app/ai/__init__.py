"""Phase D: reading Amadeus screenshots with Claude (docs/design/11-screenshots.md).

* ``client``: the ``TranscribeClient`` interface and the real implementation (Anthropic SDK).
* ``fake``: a scripted client for tests (a test never calls the real API).
* ``pricing``: the price table and the cost of a call in micro-USD.
* ``budget``: the agency's monthly budget, the usage log (``ai_usage``) and the check before a call.
* ``images``: validation, metadata removal and downscaling of an uploaded image, in memory.
* ``fallback``: when a cheap transcript is not good enough and the stronger model reads it again.
* ``screenshots``: the whole read of one submit (up to 4 images) into Amadeus text.

Nothing here stores or logs an image, a transcript or a name.
"""
