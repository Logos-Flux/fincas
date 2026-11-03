"""Golden facts -> Google Ads request bodies, and responses back. Pure functions.

Every function is deterministic: the same engagement and click give the
same JSON, byte for byte, so a replayed upload is identical and the fixture
comparison is exact. Only the fields 3.5 names are sent: the click id, the
conversion action, the close time, the engagement number, the currency, the
value when known, and the ``ad_user_data`` consent. No name, address, email
or phone (enhanced conversions are out of scope).
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

from . import values

_DIGITS = re.compile(r"^\d{10}$")
_ACTION = re.compile(r"^\d{1,20}$")
_PIPELINE_KEY = re.compile(r"^[-a-z0-9_]{1,32}$")


def format_datetime(moment: dt.datetime) -> str:
    """Google's ``yyyy-mm-dd hh:mm:ss+hh:mm``, always in UTC."""
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")


def customer_id(settings: dict) -> str:
    return str(settings.get("customer_id", "")).replace("-", "")


def action_resource(settings: dict, pipeline_key: str) -> str:
    """``customers/<id>/conversionActions/<action>`` for the pipeline, or "" (unmapped)."""
    action = (settings.get("conversion_actions") or {}).get(pipeline_key)
    if not action:
        return ""
    return f"customers/{customer_id(settings)}/conversionActions/{action}"


def consent(settings: dict) -> str:
    return settings.get("ad_user_data_consent") or values.DEFAULT_CONSENT


def click_window(settings: dict) -> dt.timedelta:
    return dt.timedelta(days=settings.get("click_window_days", values.DEFAULT_CLICK_WINDOW_DAYS))


def validate_settings(settings: dict) -> list[str]:
    """Problem + cause + fix for each bad key; [] when the settings are usable."""
    errors = []
    if not _DIGITS.match(customer_id(settings)):
        errors.append("customer_id: not a Google Ads customer id — cause: it must be 10 digits "
                      "(dashes allowed). Fix: copy it from the Google Ads account header.")
    login = str(settings.get("login_customer_id", "")).replace("-", "")
    if login and not _DIGITS.match(login):
        errors.append("login_customer_id: not a customer id — cause: it must be 10 digits. Fix: "
                      "use the manager account's id, or leave it out.")
    actions = settings.get("conversion_actions", {})
    if not isinstance(actions, dict) or any(
        not isinstance(k, str) or not _PIPELINE_KEY.match(k) or not _ACTION.match(str(v))
        for k, v in actions.items()
    ):
        errors.append("conversion_actions: must map pipeline keys to numeric conversion action "
                      "ids — cause: another shape was given. Fix: {\"sales\": \"123456789\"}.")
    window = settings.get("click_window_days", values.DEFAULT_CLICK_WINDOW_DAYS)
    if isinstance(window, bool) or not isinstance(window, int) or not 1 <= window <= 90:
        errors.append("click_window_days: must be a whole number of days from 1 to 90 — cause: "
                      f"{window!r}. Fix: match the conversion action's click-through window.")
    if consent(settings) not in values.CONSENT_VALUES:
        errors.append(f"ad_user_data_consent: must be one of {', '.join(values.CONSENT_VALUES)} — "
                      f"cause: {settings.get('ad_user_data_consent')!r}. Fix: leave it out for "
                      f"{values.DEFAULT_CONSENT} (OP #2246).")
    return errors


def conversion_row(*, click_kind: str, click_id: str, action: str, closed_at: dt.datetime,
                   order_id: str, currency: str, value: Decimal | None, consent_value: str) -> dict:
    """One click conversion. A null ``value`` sends no ``conversionValue`` at
    all (Google then uses the action's default): never zero, never converted."""
    row = {
        click_kind: click_id,
        "conversionAction": action,
        "conversionDateTime": format_datetime(closed_at),
        "orderId": order_id,
        "currencyCode": currency,
        "consent": {"adUserData": consent_value},
    }
    if value is not None:
        row["conversionValue"] = float(value)
    return row


def retraction_row(*, action: str, order_id: str, at: dt.datetime) -> dict:
    return {
        "conversionAction": action,
        "adjustmentType": "RETRACTION",
        "orderId": order_id,
        "adjustmentDateTime": format_datetime(at),
    }


def request_body(event: str, rows: list[dict]) -> dict:
    key = "conversions" if event == values.UPLOAD else "conversionAdjustments"
    return {key: rows, "partialFailure": True}


def request_path(event: str, settings: dict) -> str:
    template = values.UPLOAD_PATH if event == values.UPLOAD else values.ADJUST_PATH
    return template.format(version=values.API_VERSION, customer_id=customer_id(settings))


def failed_rows(payload: dict) -> dict[int, str]:
    """``{row index: error code}`` from a partial-failure response.

    Only the code is kept (``CLICK_NOT_FOUND``): Google's messages can quote
    the request, and nothing from a provider body goes into an outbox row.
    """
    failure = (payload or {}).get("partialFailureError") or {}
    failed: dict[int, str] = {}
    unlocated = False
    for detail in failure.get("details") or []:
        for error in (detail or {}).get("errors") or []:
            index = None
            for element in ((error.get("location") or {}).get("fieldPathElements") or []):
                if element.get("fieldName") in ("conversions", "conversion_adjustments",
                                                "conversionAdjustments") and "index" in element:
                    index = element["index"]
                    break
            if not isinstance(index, int):
                unlocated = True
                continue
            codes = error.get("errorCode") or {}
            code = next((str(v) for v in codes.values()), "UNKNOWN") if isinstance(codes, dict) else "UNKNOWN"
            failed.setdefault(index, re.sub(r"[^A-Z0-9_]", "", code.upper())[:64] or "UNKNOWN")
    if failure and (unlocated or not failed):
        # An error with no row location: no row can be trusted as accepted,
        # so every row without its own code retries.
        failed[-1] = "PARTIAL_FAILURE_UNLOCATED"
    return failed
