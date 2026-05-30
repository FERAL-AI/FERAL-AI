""" security boundary tests.

Each module in this package demonstrates a single approval / auth /
sandbox boundary by attempting an explicit bypass and asserting:

  1. The underlying side-effect did NOT execute.
  2. The supervisor recorded a denial event with decision != "allowed".

See SECURITY.md for the in-scope categories these tests defend.
"""
