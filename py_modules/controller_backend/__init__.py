"""The Decky backend of Decky Controller: settings, the daemon supervisor, status assembly.

Talks to Decky only through what ``main.py`` injects (logger, emit, directories); never imports
``decky`` or ``deckgadget`` — the daemon runs as a subprocess so a broken core cannot take the backend down.
"""
