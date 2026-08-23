"""Read-only facts about the Steam Deck hardware, taken from sysfs.

Shared by the Decky backend and the ``deckgadget`` daemon. Nothing here writes to the kernel or issues
ioctls; everything takes the tree roots as parameters so tests can point it at a fake tree.
"""
