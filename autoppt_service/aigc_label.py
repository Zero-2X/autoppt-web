"""Implicit labels for AI-generated deliverables (人工智能生成合成内容标识办法, effective 2025-09-01; GB 45438-2025).

The rules ask providers to write an implicit label into the metadata of generated files: that the content is
AI-generated, who produced it and a content ID.  Decks get an ``AIGC`` custom document property (Office
custom properties, ``docProps/custom.xml``) holding the JSON label; nothing visible on the slides changes and
no other part of the package is rewritten, so the approved deck and the delivered deck differ only in those
metadata parts (recorded in the delivery manifest).  Text deliverables carry the label in the manifest.
Masters are never labelled in place: their hashes are the provenance record.

The field names follow GB 45438-2025's metadata label (Label, ContentProducer, ProduceID, ReservedCode1,
ContentPropagator, PropagateID, ReservedCode2).  The producer code is the operator's registration code once
the service is filed (备案); until then it is the service name.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

CUSTOM_PART = "docProps/custom.xml"
CUSTOM_TYPE = "application/vnd.openxmlformats-officedocument.custom-properties+xml"
CUSTOM_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties"
FMTID = "{D5CDD505-2E9C-101B-9397-08002B2CF9AE}"
CHANGED_PARTS = (CUSTOM_PART, "[Content_Types].xml", "_rels/.rels")


def label(produce_id: str, *, producer: str | None = None) -> dict[str, str]:
    return {
        "Label": "1",  # 1 = AI-generated
        "ContentProducer": producer or os.getenv("AUTOPPT_AIGC_PRODUCER", "AutoPPT"),
        "ProduceID": produce_id,
        "ReservedCode1": "",
        "ContentPropagator": "",
        "PropagateID": "",
        "ReservedCode2": "",
    }


def _custom_xml(value: str, existing: str | None) -> str:
    prop = (f'<property fmtid="{FMTID}" pid="{{pid}}" name="AIGC"><vt:lpwstr>{escape(value)}</vt:lpwstr></property>')
    if existing and "<Properties" in existing:
        if 'name="AIGC"' in existing:
            raise ValueError("deck already carries an AIGC label")
        pids = [int(p) for p in re.findall(r'pid="(\d+)"', existing)] or [1]
        return existing.replace("</Properties>", prop.replace("{pid}", str(max(pids) + 1)) + "</Properties>")
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" '
            'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
            + prop.replace("{pid}", "2") + "</Properties>")


def label_pptx(path: Path, produce_id: str, *, producer: str | None = None) -> dict[str, str]:
    """Add the ``AIGC`` custom property in place; returns the label written."""
    path = Path(path)
    data = label(produce_id, producer=producer)
    with zipfile.ZipFile(path) as source:
        names = source.namelist()
        existing = source.read(CUSTOM_PART).decode("utf-8") if CUSTOM_PART in names else None
        types = source.read("[Content_Types].xml").decode("utf-8")
        rels = source.read("_rels/.rels").decode("utf-8")
        custom = _custom_xml(json.dumps(data, ensure_ascii=False), existing)
        if f'PartName="/{CUSTOM_PART}"' not in types:
            types = types.replace("</Types>", f'<Override PartName="/{CUSTOM_PART}" ContentType="{CUSTOM_TYPE}"/></Types>')
        if CUSTOM_REL not in rels:
            rels = rels.replace("</Relationships>",
                                f'<Relationship Id="rIdAigc" Type="{CUSTOM_REL}" Target="{CUSTOM_PART}"/></Relationships>')
        replaced = {CUSTOM_PART: custom, "[Content_Types].xml": types, "_rels/.rels": rels}
        handle, temp = tempfile.mkstemp(suffix=".pptx", dir=path.parent)
        os.close(handle)
        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                if info.filename in replaced:
                    target.writestr(info, replaced.pop(info.filename).encode("utf-8"))
                else:
                    target.writestr(info, source.read(info.filename))
            for name, text in replaced.items():  # docProps/custom.xml when the deck had none
                target.writestr(name, text.encode("utf-8"))
    shutil.move(temp, path)
    return data


def read_pptx_label(path: Path) -> dict[str, str] | None:
    with zipfile.ZipFile(path) as package:
        if CUSTOM_PART not in package.namelist():
            return None
        match = re.search(r'name="AIGC"><vt:lpwstr>(.*?)</vt:lpwstr>', package.read(CUSTOM_PART).decode("utf-8"), re.S)
    if not match:
        return None
    from xml.sax.saxutils import unescape

    return json.loads(unescape(match.group(1)))


def changed_parts(before: Path, after: Path) -> list[str]:
    """Package parts whose bytes differ between two decks (the label should touch only CHANGED_PARTS)."""
    with zipfile.ZipFile(before) as a, zipfile.ZipFile(after) as b:
        names = set(a.namelist()) | set(b.namelist())
        return sorted(n for n in names if n not in a.namelist() or n not in b.namelist() or a.read(n) != b.read(n))
