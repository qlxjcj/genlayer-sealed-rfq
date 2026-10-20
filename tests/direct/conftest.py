"""Fixtures for Sealed RFQ direct-mode tests.

Direct mode does not track native value flow, so two accounting hooks are
installed (mirroring the chain) so the tests can assert real fund movement:

1. payable value: `open_rfq` / `bid` move `vm.value` sender -> contract
2. `emit_transfer` is intercepted as a PostMessage gl_call and moved
   contract -> recipient

A refused payable call restores the value, so "a refusal moves nothing" is
testable for exactly the calls that matter (bond posted, budget escrowed).

Time is driven explicitly with `vm.warp(iso)`, and every deadline is an absolute
epoch, which is what the contract compares against.
"""

import hashlib
import json
import os
from datetime import datetime, timezone

import pytest

CONTRACT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "sealed_rfq.py",
)

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
EPOCH0 = int(BASE.timestamp())

BUDGET = 10 ** 18
BOND = 10 ** 16
PRICE_A = 6 * 10 ** 17
PRICE_B = 7 * 10 ** 17
PRICE_C = 8 * 10 ** 17

BID_CLOSE = EPOCH0 + 100
REVEAL_CLOSE = EPOCH0 + 200
WINDOW = 100
ORDER = "PO-4711"
MARKER = "ACME-7"

BUYER = "0x" + "aa" * 20
ALICE = "0x" + "b1" * 20
BOB = "0x" + "b2" * 20
CAROL = "0x" + "b3" * 20
STRANGER = "0x" + "44" * 20

URL_A = "https://alpha.example.com/records"
URL_B = "https://bravo.example.com/ledger"

# The record has to name the order *and* report it done; both facts are checked
# against the fetched body, so the model cannot invent either.
SPAN_OK = "PO-4711: ACME-7 delivered to dock 3."
BODY_OK = "Delivery records\n" + SPAN_OK + "\n"
BODY_PLAN = "Delivery records\nPO-4711: ACME-7 planned for March.\n"
BODY_NO_REF = "Delivery records\nACME-7 delivered to dock 3.\n"
BODY_OTHER = "Delivery records\nPO-9999: ACME-7 delivered to dock 3.\n"
BODY_EMPTY = ""
BODY_ERROR = "Internal Server Error"


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def balance(vm, addr):
    return vm._balances.get(vm._to_bytes(addr), 0)


def contract_balance(vm):
    return vm._balances.get(vm._contract_address, 0)


def answer(span):
    return json.dumps({"span": span})


def remock(vm, body_a=BODY_OK, body_b=BODY_OK, span=SPAN_OK, status_a=200, status_b=200):
    """Mocks are first-match-wins, so always clear before installing.

    The model answer is keyed on the marker; whether it is *usable* is decided
    by the code against the fetched body, which is what the tests pin down.
    """
    vm.clear_mocks()
    vm.mock_web(".*alpha\\.example\\.com.*", {"method": "GET", "status": status_a, "body": body_a})
    vm.mock_web(".*bravo\\.example\\.com.*", {"method": "GET", "status": status_b, "body": body_b})
    vm.mock_llm(r"MARKER: ACME-7", answer(span))


@pytest.fixture
def rfq(direct_vm, direct_deploy):
    vm = direct_vm

    def _value_transfer_hook(vm, request):
        if "PostMessage" not in request:
            return None
        msg = request["PostMessage"]
        amount = int(msg.get("value", 0))
        if amount > 0:
            contract = vm._contract_address
            recipient = vm._to_bytes(msg["address"])
            vm._balances[contract] = vm._balances.get(contract, 0) - amount
            vm._balances[recipient] = vm._balances.get(recipient, 0) + amount
        return {"ok": None}

    vm._gl_call_hook = _value_transfer_hook

    remock(vm)
    c = direct_deploy(CONTRACT)

    def _wrap(fn):
        def inner(*args):
            paid = 0
            sender = vm._to_bytes(vm.sender)
            contract = vm._contract_address
            if vm.value > 0:
                vm._balances[sender] = vm._balances.get(sender, 0) - vm.value
                vm._balances[contract] = vm._balances.get(contract, 0) + vm.value
                paid = vm.value
            try:
                return fn(*args)
            except Exception:
                if paid:
                    vm._balances[sender] = vm._balances.get(sender, 0) + paid
                    vm._balances[contract] = vm._balances.get(contract, 0) - paid
                raise
        return inner

    c.open_rfq = _wrap(c.open_rfq)
    c.bid = _wrap(c.bid)

    for addr in (BUYER, ALICE, BOB, CAROL, STRANGER):
        vm.deal(addr, 100 * BUDGET)

    vm.warp(iso(EPOCH0))
    vm.sender = BUYER
    return vm, c


def open_rfq(vm, c, budget=BUDGET, ref=ORDER, marker=MARKER, sources=None,
             bid_until=BID_CLOSE, reveal_until=REVEAL_CLOSE, window=WINDOW, bond=BOND,
             sender=BUYER):
    vm.warp(iso(EPOCH0))
    vm.sender = sender
    vm.value = budget if budget is not None else BUDGET
    try:
        return c.open_rfq(ref, marker, [URL_A, URL_B] if sources is None else sources,
                          bid_until, reveal_until, window, bond)
    finally:
        vm.value = 0


def commit_for(price, salt="s1"):
    """The exact preimage the contract hashes: "<price>|<salt>" in sha256."""
    return hashlib.sha256((str(int(price)) + "|" + str(salt)).encode("utf-8")).hexdigest()


def post_bid(vm, c, rid, who, price, salt="s1", t=None, value=None, commit=None):
    if t is None:
        t = EPOCH0 + 50
    vm.warp(iso(t))
    vm.sender = who
    vm.value = BOND if value is None else value
    try:
        return c.bid(rid, commit_for(price, salt) if commit is None else commit)
    finally:
        vm.value = 0


def reveal(vm, c, rid, who, price, salt="s1", t=None):
    if t is None:
        t = EPOCH0 + 150
    vm.warp(iso(t))
    vm.sender = who
    return c.reveal(rid, price, salt)


def award(vm, c, rid, t=None):
    if t is None:
        t = REVEAL_CLOSE + 1
    vm.warp(iso(t))
    vm.sender = STRANGER
    return c.open_award(rid)


def check(vm, c, rid, t):
    vm.warp(iso(t))
    vm.sender = STRANGER
    return c.check_delivery(rid)


def state(vm, c, rid):
    return json.loads(c.get_rfq(rid))


def bid_state(vm, c, rid, who):
    return json.loads(c.get_bid(rid, who))


def order(vm, c, rid):
    return json.loads(c.get_order(rid))["order"]


def three_bidders(vm, c, rid):
    post_bid(vm, c, rid, ALICE, PRICE_A)
    post_bid(vm, c, rid, BOB, PRICE_B)
    post_bid(vm, c, rid, CAROL, PRICE_C)
