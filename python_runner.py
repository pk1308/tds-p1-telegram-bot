"""Restricted Python runner executed in a subprocess by tools.run_python()."""
import json
import sys
import traceback
import math
import statistics
import random
import datetime
import re
import csv
import io
import collections
import itertools
import fractions
import decimal
import typing
import hashlib
import base64
import urllib.request
import urllib.parse
import urllib.error
import warnings

# Optional third-party packages used for data analysis; ignored if not installed.
try:
    import pandas as pd
except ImportError:
    pd = None
try:
    import pypdf
except ImportError:
    pypdf = None

warnings.filterwarnings("ignore")

_locals = {}

BUILTINS = {
    "True": True, "False": False, "None": None,
    "abs": abs, "all": all, "any": any, "bin": bin, "bool": bool,
    "bytes": bytes, "chr": chr, "complex": complex, "dict": dict,
    "divmod": divmod, "enumerate": enumerate, "filter": filter,
    "float": float, "format": format, "frozenset": frozenset,
    "hasattr": hasattr, "hex": hex, "int": int, "isinstance": isinstance,
    "issubclass": issubclass, "iter": iter, "len": len, "list": list,
    "map": map, "max": max, "min": min, "next": next, "oct": oct,
    "ord": ord, "pow": pow, "print": print, "range": range, "repr": repr,
    "reversed": reversed, "round": round, "set": set, "slice": slice,
    "sorted": sorted, "str": str, "sum": sum, "tuple": tuple, "type": type,
    "zip": zip,
    "json": json, "math": math, "statistics": statistics, "csv": csv,
    "io": io, "re": re, "datetime": datetime, "collections": collections,
    "itertools": itertools, "fractions": fractions, "decimal": decimal,
    "typing": typing, "hashlib": hashlib, "base64": base64, "urllib": urllib,
    "random": random,
}
if pd is not None:
    BUILTINS["pd"] = pd
if pypdf is not None:
    BUILTINS["pypdf"] = pypdf


def _run():
    code_path = sys.argv[1]
    code = open(code_path, "r", encoding="utf-8").read()
    try:
        exec(code, {"__builtins__": BUILTINS}, _locals)
        last_expr = code.strip().split("\n")[-1].strip()
        if last_expr and not last_expr.startswith((" ", "\t", "import ", "from ", "def ", "class ", "if ", "for ", "while ", "try:", "with ", "return", "print", "#")):
            try:
                result = eval(compile(last_expr, "<last_expr>", "eval"), {"__builtins__": BUILTINS}, _locals)
                if result is not None:
                    print("\n__RESULT__", json.dumps(result, ensure_ascii=False, default=str))
            except Exception:
                pass
    except Exception as e:
        print("__ERROR__", type(e).__name__, str(e), file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _run()
