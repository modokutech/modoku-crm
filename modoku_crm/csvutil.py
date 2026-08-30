"""Shared CSV-export helper — every module's list page can export its
current (filtered) rows as a CSV file in a couple of lines, reusing the same
RFC 6266/5987-safe filename header the PDF downloads already use.
"""
import csv
import io

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
