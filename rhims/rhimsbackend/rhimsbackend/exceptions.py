"""
Project-wide DRF exception handler.

WHY THIS FILE EXISTS
---------------------
Almost every model in this project (MedicineBatch, SupplyBatch, Dealer,
DealerTransaction, PharmacyBill, StaffProfile, ... ) overrides save() to call
self.full_clean(), e.g.:

    def save(self, *args, **kwargs):
        ...
        self.full_clean()
        super().save(*args, **kwargs)

full_clean() raises django.core.exceptions.ValidationError — NOT
rest_framework.exceptions.ValidationError. DRF's default exception_handler
only converts Http404, PermissionDenied, and its own APIException subclasses;
anything else (including Django's ValidationError, and IntegrityError from
the DB driver) falls through and Django returns a bare 500 Internal Server
Error with no useful JSON body.

This is why POST/PATCH endpoints like /api/pharmacist/batches/create/ return
500 instead of a proper 400 the moment a save() fails validation — duplicate
batch number, cost_price > mrp, allocated_quantity > quantity, a blank
required field, a dealer FK that doesn't exist at the DB level, etc. GET
endpoints are unaffected because they don't call save().

This handler plugs that gap for every app (pharmacist, manager, doctor, lab,
reception, administration) in one place, so individual views don't each need
a try/except around every .save() call.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.http import Http404
from rest_framework.views import exception_handler as drf_exception_handler
from rest_framework.response import Response
from rest_framework import status
import re

# Friendlier wording for constraint names we know about. Falls back to a
# generic-but-still-useful message (raw key name + values) for anything else,
# so a brand new constraint added later never goes back to a silent 409.
KNOWN_CONSTRAINTS = {
    "unique_batch_number_per_medicine": "A batch with this batch number already exists for this medicine. Use a different batch number, or edit the existing batch instead.",
    "medicine_default_route_not_empty": "Default route cannot be empty for this medicine.",
}

_DUPLICATE_RE = re.compile(r"Duplicate entry '(?P<value>.*?)' for key '(?P<key>[\w.`]+)'")
_FK_RE = re.compile(r"a foreign key constraint fails.*?REFERENCES `(?P<table>\w+)`")


def _describe_integrity_error(exc):
    """Best-effort translation of a MySQL IntegrityError into a clear message."""
    msg = str(exc)

    dup = _DUPLICATE_RE.search(msg)
    if dup:
        key = dup.group("key").split(".")[-1].strip("`")
        value = dup.group("value")
        friendly = KNOWN_CONSTRAINTS.get(key)
        if friendly:
            return friendly
        return f"A record with the value '{value}' already exists (conflicts with '{key}')."

    fk = _FK_RE.search(msg)
    if fk:
        return f"This references a {fk.group('table')} record that doesn't exist. Double-check the selected value and try again."

    # SECURITY: don't echo the raw DB message to the client — it can contain
    # table/column names, constraint internals, or fragments of the query.
    # Full detail still goes to the server log; the client gets a generic,
    # actionable message. In DEBUG we keep the raw message since that's a
    # trusted developer environment.
    from django.conf import settings
    import logging
    logging.getLogger(__name__).warning("Unrecognized IntegrityError: %s", msg)
    if settings.DEBUG:
        return f"This action conflicts with existing data: {msg}"
    return "This action conflicts with existing data. Please check the values and try again."


def custom_exception_handler(exc, context):
    # Let DRF handle everything it already knows about (APIException,
    # Http404, PermissionDenied, DRF's own ValidationError, etc).
    response = drf_exception_handler(exc, context)
    if response is not None:
        return response

    # Django's model-level ValidationError (from full_clean()/clean()) —
    # this is the main source of the "500 on create/update" bug.
    if isinstance(exc, DjangoValidationError):
        if hasattr(exc, "message_dict"):
            detail = exc.message_dict
        elif hasattr(exc, "messages"):
            detail = {"detail": exc.messages}
        else:
            detail = {"detail": str(exc)}
        return Response(detail, status=status.HTTP_400_BAD_REQUEST)

    # DB-level integrity errors (e.g. a UniqueConstraint that IS enforced at
    # the DB but wasn't caught by full_clean(), or a race condition between
    # the uniqueness check and the INSERT) — surface as 409 instead of 500,
    # with the actual conflicting field/value instead of a black-box message.
    if isinstance(exc, IntegrityError):
        return Response(
            {"detail": _describe_integrity_error(exc)},
            status=status.HTTP_409_CONFLICT,
        )

    # Anything else: no response, DRF/Django falls back to its normal
    # 500 handling (and, with DEBUG=True, the usual traceback page).
    return None