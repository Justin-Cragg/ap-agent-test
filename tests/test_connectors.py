"""Connector request/response shapes against mocked HTTP endpoints."""

import base64
import json
from datetime import date
from decimal import Decimal

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ap_agent.erp.dynamics import DynamicsBCERP
from ap_agent.erp.netsuite import NetSuiteERP
from ap_agent.erp.quickbooks import QuickBooksERP
from ap_agent.erp.xero import XeroERP
from ap_agent.mail.gmail import GmailMailbox
from ap_agent.mail.outlook import OutlookMailbox
from ap_agent.models import BillDraft, BillLine, BillStatus


def _token(**extra):
    return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600, **extra})


@respx.mock
def test_netsuite_m2m_jwt_and_po_transform():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    erp = NetSuiteERP("1234567_SB1", "cid", "certid", pem)
    base = "https://1234567-sb1.suitetalk.api.netsuite.com/services/rest"

    token_route = respx.post(f"{base}/auth/oauth2/v1/token").mock(return_value=_token())
    respx.post(f"{base}/query/v1/suiteql").mock(return_value=httpx.Response(200, json={"items": [{"id": "77"}]}))
    respx.get(f"{base}/record/v1/purchaseOrder/77").mock(return_value=httpx.Response(200, json={
        "entity": {"id": "12"}, "currency": {"refName": "USD"}, "status": {"refName": "Pending Bill"},
        "item": {"items": [{"line": 1, "item": {"refName": "WID-10"}, "description": "Widget", "quantity": 10, "rate": 5, "quantityReceived": 8, "quantityBilled": 0}]},
    }))
    transform = respx.post(f"{base}/record/v1/purchaseOrder/77/!transform/vendorBill").mock(return_value=httpx.Response(204, headers={"Location": f"{base}/record/v1/vendorBill/555"}))

    po = erp.get_purchase_order("PO-77")
    assert po.vendor_id == "12" and po.lines[0].quantity_received == 8 and po.supports_receiving

    form = dict(x.split("=", 1) for x in token_route.calls[0].request.content.decode().split("&"))
    claims = jwt.decode(form["client_assertion"], key.public_key(), algorithms=["PS256"], audience=f"{base}/auth/oauth2/v1/token")
    assert claims["iss"] == "cid" and jwt.get_unverified_header(form["client_assertion"])["kid"] == "certid"

    bill = erp.create_bill(BillDraft(vendor_id="12", invoice_number="INV-1", invoice_date=date(2026, 11, 1), total=Decimal("40"), po_id="77", po_number="PO-77", lines=[BillLine(description="Widget", amount=Decimal("40"), quantity=Decimal(8), unit_price=Decimal(5), po_line_id="1")]))
    assert bill.id == "555"
    body = json.loads(transform.calls[0].request.content)
    assert body["tranId"] == "INV-1" and body["item"]["items"] == [{"orderLine": 1, "quantity": 8.0, "rate": 5.0}]


@respx.mock
def test_dynamics_bc_po_and_invoice_post():
    erp = DynamicsBCERP("tenant", "cid", "secret", "production", "comp-1")
    base = "https://api.businesscentral.dynamics.com/v2.0/tenant/production/api/v2.0/companies(comp-1)"
    respx.post("https://login.microsoftonline.com/tenant/oauth2/v2.0/token").mock(return_value=_token())
    respx.get(f"{base}/purchaseOrders").mock(return_value=httpx.Response(200, json={"value": [{"id": "po1", "number": "PO-9", "vendorId": "v1", "currencyCode": "", "status": "Open", "purchaseOrderLines": [
        {"id": "l1", "lineType": "Item", "lineObjectNumber": "1000", "description": "Bike", "quantity": 4, "directUnitCost": 100, "receivedQuantity": 4, "invoicedQuantity": 1},
        {"id": "l2", "lineType": "Comment", "description": "note"},
    ]}]}))
    create = respx.post(f"{base}/purchaseInvoices").mock(return_value=httpx.Response(201, json={"id": "pi1"}))
    lines = respx.post(f"{base}/purchaseInvoices(pi1)/purchaseInvoiceLines").mock(return_value=httpx.Response(201, json={}))
    post = respx.post(f"{base}/purchaseInvoices(pi1)/Microsoft.NAV.post").mock(return_value=httpx.Response(204))

    po = erp.get_purchase_order("PO-9")
    assert len(po.lines) == 1 and po.lines[0].quantity_billed == 1
    erp.create_bill(BillDraft(vendor_id="v1", invoice_number="X1", invoice_date=date(2026, 11, 2), total=Decimal("75"), lines=[BillLine(description="Consulting", amount=Decimal("75"), gl_account="6500")]))
    assert json.loads(create.calls[0].request.content)["vendorInvoiceNumber"] == "X1"
    assert json.loads(lines.calls[0].request.content) == {"lineType": "Account", "lineObjectNumber": "6500", "description": "Consulting", "quantity": 1.0, "directUnitCost": 75.0}
    assert post.called


@respx.mock
def test_quickbooks_rotates_refresh_token_and_links_po():
    store = {}
    erp = QuickBooksERP("cid", "sec", "999", "rt-initial", store.get, store.__setitem__, sandbox=True)
    respx.post("https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer").mock(return_value=_token(refresh_token="rt-rotated"))
    bill_route = respx.post("https://sandbox-quickbooks.api.intuit.com/v3/company/999/bill").mock(return_value=httpx.Response(200, json={"Bill": {"Id": "b9", "VendorRef": {"value": "56"}, "DocNumber": "A-1", "TxnDate": "2026-11-01", "TotalAmt": 50, "Balance": 50}}))

    bill = erp.create_bill(BillDraft(vendor_id="56", invoice_number="A-1", invoice_date=date(2026, 11, 1), total=Decimal("50"), po_id="po3", po_number="1003", lines=[BillLine(description="Paper", amount=Decimal("50"), gl_account="7", po_line_id="1")]))
    assert bill.status == BillStatus.open and store["quickbooks_refresh_token"] == "rt-rotated"
    line = json.loads(bill_route.calls[0].request.content)["Line"][0]
    assert line["LinkedTxn"] == [{"TxnId": "po3", "TxnType": "PurchaseOrder", "TxnLineId": "1"}]
    assert line["AccountBasedExpenseLineDetail"]["AccountRef"]["value"] == "7"


@respx.mock
def test_xero_custom_connection_and_bill_query():
    erp = XeroERP("cid", "sec", "", "", lambda k: None, lambda k, v: None)
    tok = respx.post("https://identity.xero.com/connect/token").mock(return_value=_token())
    inv = respx.get("https://api.xero.com/api.xro/2.0/Invoices").mock(return_value=httpx.Response(200, json={"Invoices": [{"InvoiceID": "i1", "Contact": {"ContactID": "c1"}, "InvoiceNumber": "S-9", "DateString": "2026-10-01T00:00:00", "DueDate": "/Date(1764547200000+0000)/", "Total": 99.5, "AmountDue": 0, "Status": "PAID"}]}))
    [bill] = erp.find_bills(vendor_id="c1", invoice_number="S-9")
    assert b"client_credentials" in tok.calls[0].request.content
    assert 'Type=="ACCPAY"' in inv.calls[0].request.url.params["where"]
    assert bill.status == BillStatus.paid and bill.invoice_date == date(2026, 10, 1) and bill.due_date == date(2025, 12, 1)


@respx.mock
def test_gmail_reads_attachments_and_labels():
    mb = GmailMailbox("ap@example.com", lambda: "tok")
    base = "https://gmail.googleapis.com/gmail/v1/users/ap@example.com"
    enc = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
    respx.get(f"{base}/messages").mock(return_value=httpx.Response(200, json={"messages": [{"id": "g1"}]}))
    respx.get(f"{base}/messages/g1").mock(return_value=httpx.Response(200, json={"id": "g1", "threadId": "th", "internalDate": "1700000000000", "payload": {
        "headers": [{"name": "From", "value": "Acme <billing@acme.example>"}, {"name": "Subject", "value": "Invoice 7"}, {"name": "To", "value": "ap@example.com"}],
        "parts": [{"partId": "0", "mimeType": "text/plain", "body": {"data": enc(b"See attached")}}, {"partId": "1", "mimeType": "application/pdf", "filename": "inv7.pdf", "body": {"attachmentId": "att1"}}],
    }}))
    respx.get(f"{base}/messages/g1/attachments/att1").mock(return_value=httpx.Response(200, json={"data": enc(b"%PDF-1.7")}))
    respx.get(f"{base}/labels").mock(return_value=httpx.Response(200, json={"labels": []}))
    respx.post(f"{base}/labels").mock(return_value=httpx.Response(200, json={"id": "Label_9"}))
    modify = respx.post(f"{base}/messages/g1/modify").mock(return_value=httpx.Response(200, json={}))

    [msg] = mb.list_new_messages()
    assert msg.sender == "billing@acme.example" and msg.body_text == "See attached"
    assert msg.attachments[0].data == b"%PDF-1.7"
    mb.label(msg, "AP-Processed")
    assert json.loads(modify.calls[0].request.content) == {"addLabelIds": ["Label_9"]}


@respx.mock
def test_outlook_skips_processed_and_drafts_reply():
    mb = OutlookMailbox("ap@example.com", lambda: "tok")
    base = "https://graph.microsoft.com/v1.0/users/ap@example.com"
    respx.get(f"{base}/mailFolders/inbox/messages").mock(return_value=httpx.Response(200, json={"value": [
        {"id": "o1", "conversationId": "c", "from": {"emailAddress": {"address": "ar@vendor.example"}}, "toRecipients": [], "subject": "Inv", "body": {"contentType": "html", "content": "<p>Hello<br>attached</p>"}, "receivedDateTime": "2026-11-20T10:00:00Z", "hasAttachments": True, "categories": []},
        {"id": "o2", "subject": "old", "categories": ["AP-Processed"]},
    ]}))
    respx.get(f"{base}/messages/o1/attachments").mock(return_value=httpx.Response(200, json={"value": [{"@odata.type": "#microsoft.graph.fileAttachment", "id": "a", "name": "i.pdf", "contentType": "application/pdf", "contentBytes": base64.b64encode(b"%PDF").decode(), "isInline": False}]}))
    draft = respx.post(f"{base}/messages/o1/createReply").mock(return_value=httpx.Response(201, json={"id": "d1"}))

    msgs = mb.list_new_messages()
    assert [m.id for m in msgs] == ["o1"] and "Hello" in msgs[0].body_text and msgs[0].attachments[0].filename == "i.pdf"
    assert mb.reply(msgs[0], "Thanks", send=False) == "d1"
    assert json.loads(draft.calls[0].request.content) == {"comment": "Thanks"}


@pytest.mark.parametrize("provider,cls", [("bedrock", "AnthropicBedrockMantle"), ("vertex", "AnthropicVertex"), ("foundry", "AnthropicFoundry"), ("anthropic", "Anthropic")])
def test_llm_provider_selection(provider, cls, settings, monkeypatch):
    from ap_agent.llm import client as llm_client

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    s = settings.model_copy(update={"llm_provider": provider, "vertex_project_id": "p", "foundry_resource": "r", "foundry_api_key": "k"})
    llm = llm_client.LLM(s)
    assert type(llm.client).__name__ == cls
    assert llm.model == ("anthropic.claude-opus-5-5" if provider == "bedrock" else "claude-opus-5-5")
    params = llm._common({"messages": []})
    if provider == "anthropic":
        assert params["fallbacks"] == "default" and llm.new_conversation_state() is None
    else:  # client-side refusal fallback middleware instead
        assert "fallbacks" not in params and llm.new_conversation_state() is not None
