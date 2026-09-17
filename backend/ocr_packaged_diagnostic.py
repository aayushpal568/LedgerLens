import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(__file__))

from engine.providers.ocr import PaddleOCRProvider

path = sys.argv[1]
provider = PaddleOCRProvider()
print(json.dumps({"available": provider.available, "path": path, "frozen": getattr(sys, "frozen", False)}))
try:
    engine = provider._load()
    print(json.dumps({"engine_type": type(engine).__name__, "has_predict": hasattr(engine, "predict"), "has_ocr": hasattr(engine, "ocr")}))
except Exception:
    traceback.print_exc()
    raise SystemExit(2)

try:
    result = engine.predict(path)
    print("PREDICT_TYPE", type(result).__name__)
    for i, item in enumerate(result or []):
        print("ITEM", i, type(item).__name__, repr(item)[:4000])
except Exception:
    print("PREDICT_ERROR")
    traceback.print_exc()

try:
    result = engine.ocr(path)
    print("OCR_TYPE", type(result).__name__, repr(result)[:4000])
except Exception:
    print("OCR_ERROR")
    traceback.print_exc()
