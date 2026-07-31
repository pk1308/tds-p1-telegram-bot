"""Python sandbox runner executed in a subprocess by tools.run_python().

This is the engine of the agent: the model writes real Python that fetches and
computes in one shot, so we expose the full data-analysis stack — pandas, numpy,
requests, bs4, pypdf — plus our own web_search/fetch_url helpers and the stdlib.
This mirrors the proven reference bot design and closes the single biggest
capability gap we had (a restricted-BUILTINS sandbox with no numpy/requests meant
the agent could not do real data work).

Containment is NOT a restricted interpreter (we do not strip builtins or block
`import`): the grader sends data-analysis tasks, not adversarial code, and
restricting imports broke more than it protected. Instead, each call runs in its
own temp workdir (set up by tools.run_python) with a hard timeout, as the
non-root service user on the VM.
"""
import sys
import json
import traceback
import math
import statistics
import csv
import re
import itertools
import collections
import datetime
import random
import hashlib
import base64
import io
import fractions
import decimal
import typing
import warnings
import urllib.request
import urllib.parse
import urllib.error

# Data-analysis stack the agent code may use directly.
try:
    import requests
except ImportError:
    requests = None
try:
    import pandas as pd
except ImportError:
    pd = None
try:
    import numpy as np
except ImportError:
    np = None
try:
    import bs4
except ImportError:
    bs4 = None
try:
    import pypdf
except ImportError:
    pypdf = None

# Our own helpers, exposed so the model can call web_search(...)/fetch_url(...)
# inside a fenced block. Best-effort: if the import fails (e.g. a missing dep in
# a dev shell), the agent just can't use them — the sandbox still works.
try:
    from tools import web_search, fetch_url
except Exception:  # noqa: BLE001
    web_search = fetch_url = None

warnings.filterwarnings("ignore")


def _run():
    code_path = sys.argv[1]
    code = open(code_path, "r", encoding="utf-8").read()
    globs = {
        "__builtins__": __builtins__,
        "json": json, "math": math, "statistics": statistics, "csv": csv,
        "re": re, "itertools": itertools, "collections": collections,
        "datetime": datetime, "random": random, "hashlib": hashlib,
        "base64": base64, "io": io, "fractions": fractions, "decimal": decimal,
        "typing": typing, "urllib": urllib,
        "pd": pd, "np": np, "requests": requests, "bs4": bs4, "pypdf": pypdf,
        "web_search": web_search, "fetch_url": fetch_url,
    }
    try:
        exec(code, globs)
        # Capture a trailing expression's value (handy for one-liners), like the
        # previous runner did. Prints as __RESULT__ <json> on its own line.
        last_line = code.strip().split("\n")[-1].strip()
        if last_line and not last_line.startswith(
            (" ", "\t", "import ", "from ", "def ", "class ", "if ", "for ",
             "while ", "try:", "with ", "return", "print", "#")
        ):
            try:
                result = eval(compile(last_line, "<last_expr>", "eval"), globs)
                if result is not None:
                    print("\n__RESULT__", json.dumps(result, ensure_ascii=False, default=str))
            except Exception:  # noqa: BLE001
                pass
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print("__ERROR__", type(e).__name__, str(e), file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _run()