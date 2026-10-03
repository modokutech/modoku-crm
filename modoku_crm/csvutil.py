"""Shared CSV-export helper — every module's list page can export its
current (filtered) rows as a CSV file in a couple of lines, reusing the same
RFC 6266/5987-safe filename header the PDF downloads already use.
"""
import csv
import io
import re

from flask import Response

from .docutil import content_disposition


def csv_response(filename, header, rows):
    """filename should already end in .csv. rows: an iterable of iterables
    (each item becomes one row's cells, in header order)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    # A UTF-8 BOM so Excel (which otherwise guesses ANSI/Windows-1252) opens
    # accented or non-ASCII text correctly instead of mangling it.
    body = "﻿" + buf.getvalue()
    return Response(
        body, mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": content_disposition(filename)},
    )


def class_csv_filename(session_row, suffix):
    """Fix122: Microsoft_Excel_Basic_2026-10-05_participants.csv - the first
    three words of the course and the class start date, so exports from
    different classes don't all land as the same name in Downloads."""
    words = re.findall(r"[A-Za-z0-9]+", session_row["course_title"] or "")[:3]
    parts = ["_".join(words), session_row["start_date"] or "", suffix]
    return "_".join(p for p in parts if p) + ".csv"
