"""Telugu / Hinglish input (stretch goal). Language understanding itself is the LLM's job and is
measured by eval/run_eval.py --lang te,hi. These tests check everything around it without an LLM:
the few-shot examples are valid intents, a non-English request gets exactly the same outcome as
its English version, and code that touches raw text or names (tenant check, 10% rewrite, audit)
handles Telugu/Devanagari script safely."""
import pytest
from sqlalchemy import text

from app.agent.intent import ParsedIntent
from app.agent.prompts import FEW_SHOTS
from app.db.engine import owner_engine
from app.operations.dates import resolve, today_ist
from tests.conftest import ADMIN_A, BILLING_A, PHARMACIST_A, make_orchestrator, principal

DISCOUNT_20_LAST_MONTH = {"action": "bills.discount", "scope_all": True, "discount_pct": 20,
                          "date_range": {"preset": "last_month"}}
EXPIRING_30 = {"action": "inventory.query", "date_range": {"preset": "next_n_days", "n": 30}}

EN_DISCOUNT = "Give 20% discount on all bills from last month"
TE_DISCOUNT = "Last month bills ki 20% discount ivvu"            # the brief's example
HI_DISCOUNT = "Pichhle mahine ke saare bills pe 20% discount de do"
EN_EXPIRING = "Show items expiring in the next 30 days"
TE_EXPIRING = "వచ్చే 30 రోజుల్లో expire అయ్యే items చూపించు"
TE_PENDING_SHATAM = "pending bills ki 20 శాతం discount ivvu"
TE_CROSS_TENANT = "హాస్పిటల్ బి లో paracetamol stock 100 చేయి"

INTENTS = {
    EN_DISCOUNT: DISCOUNT_20_LAST_MONTH,
    TE_DISCOUNT: DISCOUNT_20_LAST_MONTH,
    HI_DISCOUNT: DISCOUNT_20_LAST_MONTH,
    EN_EXPIRING: EXPIRING_30,
    TE_EXPIRING: EXPIRING_30,
    TE_PENDING_SHATAM: {"action": "bills.discount", "bill_status": "pending", "discount_pct": 20},
    # Worst case: the model copies the hospital name in Telugu script instead of transliterating it.
    TE_CROSS_TENANT: {"action": "inventory.update", "item_name_contains": "paracetamol", "quantity_op": "set",
                      "quantity_value": 100, "mentioned_hospital": "హాస్పిటల్ బి"},
}


@pytest.fixture
def orch():
    return make_orchestrator(INTENTS)


def raw_query_of(request_id) -> str:
    with owner_engine().begin() as c:
        return c.execute(text("SELECT raw_query FROM audit_log WHERE request_id = :r"), {"r": request_id}).scalar()


@pytest.mark.parametrize("query,answer", FEW_SHOTS)
def test_few_shot_examples_are_valid_intents(query, answer):
    ParsedIntent.model_validate_json(answer)


@pytest.mark.parametrize("query", [TE_DISCOUNT, HI_DISCOUNT])
def test_billing_manager_refused_same_as_english(orch, sql_log, query):
    en = orch.handle(principal(BILLING_A), EN_DISCOUNT)
    sql_log.clear()
    r = orch.handle(principal(BILLING_A), query)
    assert r.type == en.type == "refusal"
    assert r.refusal.code == en.refusal.code == "discount_ceiling"
    assert not any("bills" in s for s in sql_log), "refused before any bills query"
    offer = [s for s in r.refusal.suggestions if "10%" in s.label and s.kind == "query"][0]
    assert resolve("last_month", today_ist()).start.isoformat() in offer.message
    assert any(s.kind == "request_approval" for s in r.refusal.suggestions)
    assert raw_query_of(r.request_id) == query, "audit keeps the original text"


@pytest.mark.parametrize("query", [TE_DISCOUNT, HI_DISCOUNT])
def test_admin_preview_identical_to_english(orch, fresh_db, query):
    p = principal(ADMIN_A)
    en = orch.handle(p, EN_DISCOUNT)
    r = orch.handle(p, query)
    assert r.type == en.type == "preview"
    assert r.preview.rows_affected == en.preview.rows_affected > 50
    assert r.preview.date_range == en.preview.date_range
    assert r.preview.money_impact == en.preview.money_impact
    assert r.preview.requires_typed_count
    for op in (en, r):
        orch.cancel(p, op.preview.operation_id)


def test_telugu_read_identical_to_english(orch):
    p = principal(PHARMACIST_A)
    en, te = orch.handle(p, EN_EXPIRING), orch.handle(p, TE_EXPIRING)
    assert te.type == en.type == "answer"
    assert te.answer.total == en.answer.total > 0


def test_telugu_script_hospital_name_fails_closed(orch, sql_log):
    r = orch.handle(principal(ADMIN_A), TE_CROSS_TENANT)
    assert r.type == "refusal" and r.refusal.code == "cross_tenant"
    assert not any("inventory_items" in s for s in sql_log)


def test_ten_percent_offer_rewrites_shatam(orch):
    r = orch.handle(principal(BILLING_A), TE_PENDING_SHATAM)
    assert r.type == "refusal" and r.refusal.code == "discount_ceiling"
    offer = [s for s in r.refusal.suggestions if "10%" in s.label and s.kind == "query"][0]
    assert offer.message == "pending bills ki 10% discount ivvu"
