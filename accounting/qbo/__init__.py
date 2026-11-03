"""QuickBooks Online export → Django GL import.

Three layers, each independently testable:

    readers.py   turn a .csv/.xlsx file into normalized header-keyed rows
    mapping.py   translate QBO's account vocabulary into Account choices
    importer.py  build Accounts and balanced JournalEntries from those rows
"""
