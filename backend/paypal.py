"""PayPal sandbox client — Invoicing + Payouts.

Three rules this module enforces, because money bugs are silent:
  1. MONEY IS INTEGER CENTS. Floats never touch an amount. PayPal's decimal strings are
     parsed with Decimal and immediately converted to int cents.
  2. PAYOUT BATCH IDS ARE CALLER-SUPPLIED AND DETERMINISTIC. PayPal dedupes on
     sender_batch_id for 30 days, so a retried settle with the same id is a no-op instead
     of a double payment. A random id per call would pay twice on any retry.
  3. EVERY REST CALL IS OBSERVABLE. `on_call` streams each call as it happens, so the UI
     shows the real wire traffic live instead of a post-hoc list.

Shapes verified against the live sandbox on 2026-10-16:
  create  201 -> href containing the invoice id
  send    200 -> payer-view URL, invoice status SENT
  record  200 -> payment_id, invoice status MARKED_AS_PAID
  payout  201 -> batch PENDING -> PROCESSING -> per-item SUCCESS in ~25-30s
  payout to a UA receiver -> FAILED RECEIVER_COUNTRY_NOT_ALLOWED (see planning/sandbox-accounts.md)
"""
from __future__ import annotations

import base64
import inspect
import time
import uuid
from decimal import Decimal

import httpx

from config import settings


class PayPalError(RuntimeError):
    pass


# --- money helpers: the only place cents <-> PayPal strings are converted ----
def cents_to_str(cents: int) -> str:
    if not isinstance(cents, int):
        raise TypeError(f"amount must be int cents, got {type(cents).__name__}")
    if cents < 0:
        raise ValueError("amount must not be negative")
    return f"{cents // 100}.{cents % 100:02d}"


def str_to_cents(value) -> int:
    """PayPal decimal string -> int cents, via Decimal. Never float."""
    if value in (None, ""):
        return 0
    return int((Decimal(str(value)) * 100).to_integral_value())


def paid_cents_from_invoice(inv: dict) -> int:
    """How much has actually been paid on an invoice, parsed defensively.

    PayPal exposes this in more than one place depending on how the payment arrived
    (payer paid the link vs merchant recorded it), so try each known path in order.
    Every refund amount derives from this, so it must never guess high.
    """
    payments = inv.get("payments") or {}
    pa = (payments.get("paid_amount") or {}).get("value")
    if pa not in (None, ""):
        return str_to_cents(pa)
    txns = payments.get("transactions") or []
    if txns:
        return sum(str_to_cents((t.get("amount") or {}).get("value")) for t in txns)
    amount = inv.get("amount") or {}
    total = str_to_cents(amount.get("value"))
    due = (inv.get("due_amount") or {}).get("value")
    if due not in (None, "") and total:
        return max(0, total - str_to_cents(due))
    return 0


# --- stateful mock: lets the whole lifecycle run with no network --------------
class _MockPayPal:
    """In-memory PayPal. The real one's state machine, so mock mode can actually
    reach PAID and exercise the RELEASE/UNWIND branch."""

    def __init__(self):
        self.invoices: dict[str, dict] = {}
        self.payouts: dict[str, dict] = {}

    def create_invoice(self, email, amount_cents, note, item_name, currency):
        iid = "INV2-MOCK-" + uuid.uuid4().hex[:12].upper()
        self.invoices[iid] = {
            "id": iid, "status": "DRAFT",
            "amount": {"currency_code": currency, "value": cents_to_str(amount_cents)},
            "primary_recipients": [{"billing_info": {"email_address": email}}],
            "detail": {"note": note}, "payments": {},
        }
        return iid

    def send_invoice(self, iid):
        inv = self.invoices[iid]
        inv["status"] = "SENT"
        return f"https://www.sandbox.paypal.com/invoice/p/#{iid}"

    def record_payment(self, iid, amount_cents):
        inv = self.invoices[iid]
        prior = paid_cents_from_invoice(inv)
        total = str_to_cents(inv["amount"]["value"])
        now = prior + amount_cents
        inv["payments"] = {"paid_amount": {"value": cents_to_str(now)}}
        inv["status"] = "MARKED_AS_PAID" if now >= total else "PARTIALLY_PAID"
        return "EXTR-MOCK-" + uuid.uuid4().hex[:10].upper()

    def cancel_invoice(self, iid):
        self.invoices[iid]["status"] = "CANCELLED"

    def create_payout(self, sender_batch_id, items, currency):
        bid = "MOCK" + uuid.uuid4().hex[:9].upper()
        self.payouts[bid] = {
            "sender_batch_id": sender_batch_id, "created": time.monotonic(),
            "items": [{"receiver": i["email"], "amount_cents": i["amount_cents"]} for i in items],
            "currency": currency,
        }
        return {"payout_batch_id": bid, "batch_status": "PENDING"}

    def get_payout(self, bid):
        b = self.payouts[bid]
        settled = (time.monotonic() - b["created"]) > 3  # mimic the ~25s settle, compressed
        status = "SUCCESS" if settled else "PENDING"
        return {
            "batch_header": {"payout_batch_id": bid, "batch_status": status,
                             "sender_batch_header": {"sender_batch_id": b["sender_batch_id"]}},
            "items": [
                {"payout_item": {"receiver": i["receiver"],
                                 "amount": {"value": cents_to_str(i["amount_cents"]), "currency": b["currency"]}},
                 "transaction_status": status}
                for i in b["items"]
            ],
        }


class PayPalClient:
    def __init__(self, timeout: float = 30.0, on_call=None, default_origin: str = "agent"):
        self.base = settings.paypal_base_url.rstrip("/")
        self.mock = settings.use_mock_paypal
        self.on_call = on_call                 # (record: dict) -> None | awaitable
        self.default_origin = default_origin
        self._client = httpx.AsyncClient(timeout=timeout)
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._mock = _MockPayPal() if self.mock else None
        self.calls: list[dict] = []
        self._call_seq = 0

    async def aclose(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        await self.aclose()

    # --- observability ------------------------------------------------------
    async def _record(self, method, path, status, ms, origin, resource_id=None):
        self._call_seq += 1
        rec = {"call_seq": self._call_seq, "method": method, "path": path,
               "status": status, "ms": ms, "origin": origin, "resource_id": resource_id}
        self.calls.append(rec)
        if self.on_call:
            res = self.on_call(rec)
            if inspect.isawaitable(res):
                await res
        return rec

    # --- auth ---------------------------------------------------------------
    async def _access_token(self) -> str:
        # Mock mode must be network-free for ANY caller, not only for the methods that
        # happen to branch on self.mock. pre-flight asks for a token directly, and
        # minting a real one from a test run would make "USE_MOCK_PAYPAL=1 needs no
        # credentials and no network" quietly false.
        if self.mock:
            return "MOCK-TOKEN"
        if self._token and time.monotonic() < self._token_exp - 60:
            return self._token
        auth = base64.b64encode(f"{settings.paypal_client_id}:{settings.paypal_secret}".encode()).decode()
        r = await self._client.post(
            f"{self.base}/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
        )
        if r.status_code != 200:
            raise PayPalError(f"oauth {r.status_code}: {r.text[:160]}")
        j = r.json()
        self._token = j["access_token"]
        self._token_exp = time.monotonic() + int(j.get("expires_in", 32400))
        return self._token

    async def _req(self, method: str, path: str, json_body=None, origin: str | None = None):
        origin = origin or self.default_origin
        t0 = time.monotonic()
        tok = await self._access_token()
        headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
        r = await self._client.request(method, f"{self.base}{path}", json=json_body, headers=headers)
        if r.status_code == 401:  # token died early -> refresh once
            self._token = None
            headers["Authorization"] = f"Bearer {await self._access_token()}"
            r = await self._client.request(method, f"{self.base}{path}", json=json_body, headers=headers)
        ms = int((time.monotonic() - t0) * 1000)
        body = {}
        if r.content and r.headers.get("content-type", "").startswith("application/json"):
            body = r.json()
        await self._record(method, path.split("?")[0], r.status_code, ms, origin)
        if r.status_code >= 400:
            raise PayPalError(f"{method} {path} -> {r.status_code}: {str(body)[:200]}")
        return body

    # --- Invoicing ----------------------------------------------------------
    async def create_invoice(self, recipient_email: str, amount_cents: int, note: str = "",
                             item_name: str = "Contribution", currency: str = "USD",
                             origin: str | None = None) -> str:
        if self.mock:
            iid = self._mock.create_invoice(recipient_email, amount_cents, note, item_name, currency)
            await self._record("POST", "/v2/invoicing/invoices", 201, 1, origin or self.default_origin, iid)
            return iid
        payload = {
            "detail": {"currency_code": currency, "note": note},
            "primary_recipients": [{"billing_info": {"email_address": recipient_email}}],
            "items": [{"name": item_name, "quantity": "1",
                       "unit_amount": {"currency_code": currency, "value": cents_to_str(amount_cents)}}],
        }
        body = await self._req("POST", "/v2/invoicing/invoices", payload, origin)
        href = body.get("href", "")
        return href.rsplit("/", 1)[-1] if href else body.get("id", "")

    async def send_invoice(self, invoice_id: str, origin: str | None = None) -> str:
        """Returns the real payer-view URL a contributor pays at."""
        if self.mock:
            url = self._mock.send_invoice(invoice_id)
            await self._record("POST", f"/v2/invoicing/invoices/{invoice_id}/send", 200, 1,
                               origin or self.default_origin, invoice_id)
            return url
        body = await self._req("POST", f"/v2/invoicing/invoices/{invoice_id}/send",
                               {"send_to_recipient": True}, origin)
        return body.get("href", "")

    async def get_invoice(self, invoice_id: str, origin: str | None = None) -> dict:
        if self.mock:
            await self._record("GET", f"/v2/invoicing/invoices/{invoice_id}", 200, 1,
                               origin or self.default_origin, invoice_id)
            return dict(self._mock.invoices[invoice_id])
        return await self._req("GET", f"/v2/invoicing/invoices/{invoice_id}", None, origin)

    async def remind_invoice(self, invoice_id: str, subject: str = "Reminder",
                             note: str = "", origin: str | None = None) -> None:
        if self.mock:
            await self._record("POST", f"/v2/invoicing/invoices/{invoice_id}/remind", 200, 1,
                               origin or self.default_origin, invoice_id)
            return
        await self._req("POST", f"/v2/invoicing/invoices/{invoice_id}/remind",
                        {"subject": subject, "note": note}, origin)

    async def cancel_invoice(self, invoice_id: str, subject: str = "Pool cancelled",
                             note: str = "This pool did not reach its goal — nothing is owed.",
                             notify: bool = True, origin: str | None = None) -> None:
        """notify defaults to True: a product whose promise is 'everyone gets their money
        back' must not cancel someone's invoice silently."""
        if self.mock:
            self._mock.cancel_invoice(invoice_id)
            await self._record("POST", f"/v2/invoicing/invoices/{invoice_id}/cancel", 200, 1,
                               origin or self.default_origin, invoice_id)
            return
        await self._req("POST", f"/v2/invoicing/invoices/{invoice_id}/cancel",
                        {"subject": subject, "note": note,
                         "send_to_recipient": notify, "send_to_invoicer": False}, origin)

    async def record_payment(self, invoice_id: str, amount_cents: int, currency: str = "USD",
                             origin: str | None = None) -> str:
        """Mark an invoice paid. Real PayPal API (merchant records an offline payment) —
        used to simulate a contributor paying during a demo."""
        if self.mock:
            pid = self._mock.record_payment(invoice_id, amount_cents)
            await self._record("POST", f"/v2/invoicing/invoices/{invoice_id}/payments", 200, 1,
                               origin or self.default_origin, invoice_id)
            return pid
        payload = {"method": "BANK_TRANSFER", "payment_date": time.strftime("%Y-%m-%d"),
                   "amount": {"currency_code": currency, "value": cents_to_str(amount_cents)}}
        body = await self._req("POST", f"/v2/invoicing/invoices/{invoice_id}/payments", payload, origin)
        return body.get("payment_id", "")

    # --- Payouts (REST; the money-out rail for BOTH branches) ---------------
    async def create_payout(self, sender_batch_id: str, items: list[dict], currency: str = "USD",
                            subject: str = "Potluck", origin: str | None = None) -> dict:
        """items: [{'email': str, 'amount_cents': int, 'note': str?}].

        sender_batch_id MUST be deterministic for the settlement it represents (e.g.
        potluck-{pool_id}-{decision}). PayPal dedupes on it for 30 days, which is what
        makes a retried settle safe. Never generate it randomly here.
        """
        if not sender_batch_id:
            raise ValueError("sender_batch_id is required and must be deterministic")
        if self.mock:
            out = self._mock.create_payout(sender_batch_id, items, currency)
            await self._record("POST", "/v1/payments/payouts", 201, 1,
                               origin or self.default_origin, out["payout_batch_id"])
            return out
        payload = {
            "sender_batch_header": {"sender_batch_id": sender_batch_id, "email_subject": subject},
            "items": [
                {"recipient_type": "EMAIL",
                 "amount": {"value": cents_to_str(it["amount_cents"]), "currency": currency},
                 "receiver": it["email"], "note": it.get("note", subject),
                 "sender_item_id": it.get("sender_item_id", f"item-{i}")}
                for i, it in enumerate(items)
            ],
        }
        body = await self._req("POST", "/v1/payments/payouts", payload, origin)
        return body.get("batch_header", {})

    async def get_payout(self, batch_id: str, origin: str | None = None) -> dict:
        """Reconcile a batch by GET — never by re-POST."""
        if self.mock:
            await self._record("GET", f"/v1/payments/payouts/{batch_id}", 200, 1,
                               origin or self.default_origin, batch_id)
            return self._mock.get_payout(batch_id)
        return await self._req("GET", f"/v1/payments/payouts/{batch_id}", None, origin)


def get_paypal_client(on_call=None, default_origin: str = "agent") -> PayPalClient:
    return PayPalClient(on_call=on_call, default_origin=default_origin)
