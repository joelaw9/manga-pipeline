"""ComicInfo.xml (SPEC "Metadata", "Publishing").

Validation against the v2.1 schema (no lxml needed), reading fields and building a file. Copied from the bulk
converter's optimizer/comicinfo.py; `from_kavita` comes with Phase 7c.
"""

import re
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

from manga_pipeline import archive, names

XSD = Path(__file__).with_name("ComicInfo-v2.1.xsd")
XS = "{http://www.w3.org/2001/XMLSchema}"
INT = re.compile(r"^-?\d+$")


@lru_cache(maxsize=1)
def schema():
    """Element order + types, simple-type enumerations and Page attributes, read from the XSD."""
    root = ET.parse(XSD).getroot()
    types = {t.get("name"): t for t in root}
    seq = types["ComicInfo"].find(f"{XS}sequence")
    if seq is None:
        msg = f"{XSD}: the ComicInfo type has no sequence"
        raise ValueError(msg)
    elements = [(e.attrib["name"], e.get("type")) for e in seq.findall(f"{XS}element")]
    enums = {}
    for name, t in types.items():
        if t.tag == f"{XS}simpleType":
            vals = [e.get("value") for e in t.iter(f"{XS}enumeration")]
            if vals:
                enums[name] = set(vals)
    page_attrs = {a.get("name"): a.get("type") for a in types["ComicPageInfo"].findall(f"{XS}attribute")}
    return elements, enums, page_attrs


def _is_int(value):
    return bool(INT.match(value))


def _is_boolean(value):
    return value in ("true", "false", "1", "0")


def _is_rating(value):
    try:
        return Decimal(0) <= Decimal(value) <= Decimal(5)
    except InvalidOperation:
        return False


VALUE_CHECKS = {"xs:int": _is_int, "xs:long": _is_int, "xs:boolean": _is_boolean, "Rating": _is_rating}
LIST_TYPES = {"ComicPageType"}  # a space-separated list of its enumeration's values


def _type_ok(value, xsd_type, enums):
    """Return whether value is valid for a schema type.

    Checked as a type with a value check, then a list of an enumeration, then an enumeration; any other type is text
    (xs:string).
    """
    if xsd_type in VALUE_CHECKS:
        return VALUE_CHECKS[xsd_type](value)
    if xsd_type in LIST_TYPES:
        return all(v in enums[xsd_type] for v in value.split())
    if xsd_type in enums:
        return value in enums[xsd_type]
    return True  # xs:string


def field_ok(name, value):
    """Whether `name` is a ComicInfo element other than Pages and `value` is valid for its schema type."""
    elements, enums, _ = schema()
    kinds = dict(elements)
    return name in kinds and name != "Pages" and _type_ok(value, kinds[name], enums)


def validate(xml_bytes):
    """Return a list of schema violations (empty = valid)."""
    try:
        root = ET.fromstring(xml_bytes)
    except (ET.ParseError, ValueError, LookupError) as e:  # LookupError: bogus encoding declaration
        return [f"parse-error: {e}"]
    if root.tag != "ComicInfo":
        return [f"root element is {root.tag!r}, expected 'ComicInfo'"]
    elements, enums, page_attrs = schema()
    order = {name: i for i, (name, _) in enumerate(elements)}
    kinds = dict(elements)
    errors, last, seen = [], -1, set()
    for child in root:
        tag = child.tag
        if tag not in order:
            errors.append(f"unknown element {tag}")
            continue
        if tag in seen:
            errors.append(f"duplicate element {tag}")
        seen.add(tag)
        if order[tag] < last:
            errors.append(f"element {tag} out of schema order")
        last = max(last, order[tag])
        if tag == "Pages":
            errors += _page_errors(child, page_attrs, enums)
            continue
        value = (child.text or "").strip()
        if value and not _type_ok(value, kinds[tag], enums):
            errors.append(f"{tag}={value!r} is not a valid {kinds[tag]}")
    return errors


def _page_errors(pages, page_attrs, enums):
    """Return the schema violations of the Pages element.

    An element other than Page, a Page without Image, an unknown Page attribute or one whose value its type refuses.
    """
    errors = []
    for page in pages:
        if page.tag != "Page":
            errors.append(f"unknown element Pages/{page.tag}")
            continue
        if "Image" not in page.attrib:
            errors.append("Page missing required Image attribute")
        for k, v in page.attrib.items():
            if k not in page_attrs:
                errors.append(f"unknown Page attribute {k}")
            elif not _type_ok(v, page_attrs[k], enums):
                errors.append(f"Page/@{k}={v!r} is not a valid {page_attrs[k]}")
    return errors


def read_fields(xml_bytes, names=("Series", "Title", "Number", "Volume", "Count")):
    """Return {name: text} of the named top-level elements, stripped; '' for one that is absent or empty."""
    root = ET.fromstring(xml_bytes)
    return {n: (root.findtext(n) or "").strip() for n in names}


def read_all(xml_bytes):
    """Return {element: text} of every non-empty top-level element but Pages, as written (unknown elements included).

    ET.ParseError for XML that does not parse.
    """
    root = ET.fromstring(xml_bytes)
    return {
        child.tag: (child.text or "").strip() for child in root if child.tag != "Pages" and (child.text or "").strip()
    }


def read_pages(xml_bytes):
    """Return the Page entries of the Pages element, as build takes them.

    [{attribute: value}] in order; [] when there is none. ET.ParseError for XML that does not parse.
    """
    pages = ET.fromstring(xml_bytes).find("Pages")
    return [] if pages is None else [dict(page.attrib) for page in pages.findall("Page")]


def build(fields, pages=None):
    """Build ComicInfo.xml bytes with elements in schema order.

    `fields` maps element -> value (str/int); empty values are skipped; `pages` is [{"Image": 0, "Type": "FrontCover",
    ...}]. Raises if invalid.
    """
    elements, _, _ = schema()
    root = ET.Element(
        "ComicInfo",
        {"xmlns:xsd": "http://www.w3.org/2001/XMLSchema", "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance"},
    )
    unknown = set(fields) - {n for n, _ in elements}
    if unknown:
        msg = f"not ComicInfo elements: {sorted(unknown)}"
        raise ValueError(msg)
    for name, _ in elements:
        if name == "Pages":
            if pages:
                el = ET.SubElement(root, "Pages")
                for pg in pages:
                    ET.SubElement(el, "Page", {k: str(v) for k, v in pg.items()})
            continue
        value = fields.get(name)
        if value is None or value == "":
            continue
        ET.SubElement(root, name).text = str(value)
    ET.indent(root)
    xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    errors = validate(xml)
    if errors:
        msg = f"built ComicInfo is invalid: {errors}"
        raise ValueError(msg)
    return xml


def download_number(path):
    """Return a download's ComicInfo Number: '' without one, or for an archive that cannot be read."""
    try:
        xml = archive.read_comicinfo(path)
    except archive.UNREADABLE:
        return ""
    return read_fields(xml, ("Number",))["Number"] if xml else ""


def read_download(path):
    """Return (fields, item) of a download as the comicinfo stage reads it.

    The ComicInfo.xml fields valid for the schema ({} without one), and its name's parse, numbered by that Number when
    the name has none. Raises archive.UNREADABLE or ET.ParseError for an archive or a ComicInfo.xml that cannot be read.
    """
    path = Path(path)
    xml = archive.read_comicinfo(path)
    fields = {k: v for k, v in read_all(xml).items() if field_ok(k, v)} if xml else {}
    return fields, names.parse(path.stem, fields.get("Number", ""))
