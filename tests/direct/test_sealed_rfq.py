"""Direct-mode tests for Sealed RFQ.

Groups:

  A  creation    -- the budget is held whole and the judged terms are frozen
  B  bidding     -- bond, one bid per address, sealed commitments
  C  reveal      -- the price opens only with the right salt, inside the window
  D  award       -- cheapest wins, ties by address, no-shows forfeit
  E  delivery    -- two fixed sources, order reference, fail-closed
  F  cascade     -- a missed window hands the job down the queue
  G  cancel      -- the buyer's one clean exit, before reveals
  H  invariants  -- refusals mutate nothing; no unit is stranded
"""

import json

import pytest

from conftest import (
    ALICE,
    BOB,
    BOND,
    BODY_EMPTY,
    BODY_OK,
    BODY_OTHER,
    BODY_NO_REF,
    BODY_PLAN,
    BUDGET,
    BUYER,
    CAROL,
    EPOCH0,
    BID_CLOSE,
    PRICE_A,
    PRICE_B,
    PRICE_C,
    REVEAL_CLOSE,
    SPAN_OK,
    STRANGER,
    URL_A,
    URL_B,
    WINDOW,
    award,
    balance,
    bid_state,
    check,
    commit_for,
    contract_balance,
    order,
    open_rfq,
    post_bid,
    remock,
    reveal,
    state,
    three_bidders,
)

T_AWARD = REVEAL_CLOSE + 1
T_CHECK = T_AWARD + 1
T_LATE = T_AWARD + WINDOW + 1

SPAN_NO_REF = "ACME-7 delivered to dock 3."


# ==========================================================================
# A. creation
# ==========================================================================


def test_open_rfq_escrows_the_whole_budget(rfq):
    vm, c = rfq
    before = balance(vm, BUYER)
    held = contract_balance(vm)
    rid = open_rfq(vm, c)
    assert balance(vm, BUYER) == before - BUDGET
    assert contract_balance(vm) == held + BUDGET
    r = state(vm, c, rid)
    assert r["phase"] == "BIDDING"
    assert r["budget"] == str(BUDGET)
    assert r["bidders"] == "0"
    assert r["paid"] == "0"
    assert r["refunded"] == "0"


def test_open_rfq_freezes_the_judged_terms(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    r = state(vm, c, rid)
    assert r["order_ref"] == "PO-4711"
    assert r["marker"] == "ACME-7"
    assert r["sources"] == [URL_A, URL_B]
    assert r["bond"] == str(BOND)
    assert r["bid_until"] == str(BID_CLOSE)
    assert r["reveal_until"] == str(REVEAL_CLOSE)
    assert r["window"] == str(WINDOW)
    assert r["buyer"] == BUYER


@pytest.mark.parametrize("sources", [
    [URL_A],
    [URL_A, URL_B, "https://charlie.example.com/x"],
    [],
])
def test_rejects_anything_but_two_sources(rfq, sources):
    vm, c = rfq
    with pytest.raises(Exception):
        open_rfq(vm, c, sources=sources)
    assert c.get_rfq_count() == 0


def test_rejects_two_paths_on_one_host(rfq):
    vm, c = rfq
    with pytest.raises(Exception):
        open_rfq(vm, c, sources=["https://alpha.example.com/a", "https://alpha.example.com/b"])
    assert c.get_rfq_count() == 0


@pytest.mark.parametrize("bad", ["ftp://alpha.example.com/x", "file:///etc/passwd", 42])
def test_rejects_non_http_sources(rfq, bad):
    vm, c = rfq
    with pytest.raises(Exception):
        open_rfq(vm, c, sources=[bad, URL_B])
    assert c.get_rfq_count() == 0


def test_rejects_a_zero_budget_and_keeps_the_money(rfq):
    vm, c = rfq
    before = balance(vm, BUYER)
    with pytest.raises(Exception):
        open_rfq(vm, c, budget=0)
    assert balance(vm, BUYER) == before
    assert c.get_rfq_count() == 0


@pytest.mark.parametrize("kw", [
    {"ref": ""},
    {"ref": "   "},
    {"ref": "x" * 41},
    {"marker": ""},
    {"marker": "m" * 81},
])
def test_rejects_an_empty_or_oversized_judged_term(rfq, kw):
    vm, c = rfq
    with pytest.raises(Exception):
        open_rfq(vm, c, **kw)
    assert c.get_rfq_count() == 0


@pytest.mark.parametrize("kw", [
    {"reveal_until": BID_CLOSE},
    {"reveal_until": BID_CLOSE - 10},
    {"window": 59},
    {"bond": 10 ** 13},
    {"bid_until": EPOCH0 - 1},
])
def test_rejects_an_impossible_schedule_or_bond(rfq, kw):
    vm, c = rfq
    before = balance(vm, BUYER)
    with pytest.raises(Exception):
        open_rfq(vm, c, **kw)
    assert balance(vm, BUYER) == before
    assert c.get_rfq_count() == 0


# ==========================================================================
# B. bidding
# ==========================================================================


def test_bid_posts_exactly_the_bond(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    before = balance(vm, ALICE)
    held = contract_balance(vm)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    assert balance(vm, ALICE) == before - BOND
    assert contract_balance(vm) == held + BOND
    b = bid_state(vm, c, rid, ALICE)
    assert b["status"] == "PLACED"
    assert b["bond"] == str(BOND)
    assert b["price"] == ""
    assert state(vm, c, rid)["bidders"] == "1"


@pytest.mark.parametrize("value", [0, BOND - 1, BOND + 1])
def test_wrong_bond_is_refused_and_moves_nothing(rfq, value):
    vm, c = rfq
    rid = open_rfq(vm, c)
    before = balance(vm, ALICE)
    with pytest.raises(Exception):
        post_bid(vm, c, rid, ALICE, PRICE_A, value=value)
    assert balance(vm, ALICE) == before
    assert state(vm, c, rid)["bidders"] == "0"


def test_the_buyer_may_not_bid_on_its_own_rfq(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    with pytest.raises(Exception):
        post_bid(vm, c, rid, BUYER, PRICE_A)
    assert state(vm, c, rid)["bidders"] == "0"


def test_one_bid_per_address(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    with pytest.raises(Exception):
        post_bid(vm, c, rid, ALICE, PRICE_B)
    assert state(vm, c, rid)["bidders"] == "1"


def test_bidding_closes_at_the_deadline(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    with pytest.raises(Exception):
        post_bid(vm, c, rid, ALICE, PRICE_A, t=BID_CLOSE)
    assert state(vm, c, rid)["bidders"] == "0"


def test_the_bid_board_is_bounded(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    extra = ["0x" + ("%040x" % (0x1000 + i)) for i in range(16)]
    for addr in extra:
        vm.deal(addr, 10 * BUDGET)
        post_bid(vm, c, rid, addr, PRICE_A)
    assert state(vm, c, rid)["bidders"] == "16"
    with pytest.raises(Exception):
        post_bid(vm, c, rid, STRANGER, PRICE_A)
    assert state(vm, c, rid)["bidders"] == "16"


@pytest.mark.parametrize("commit", ["abc", "0x" + "ab" * 31, "zz" * 32, ""])
def test_commitment_must_be_64_hex_chars(rfq, commit):
    vm, c = rfq
    rid = open_rfq(vm, c)
    with pytest.raises(Exception):
        post_bid(vm, c, rid, ALICE, PRICE_A, commit=commit)
    assert state(vm, c, rid)["bidders"] == "0"


def test_a_refused_bid_leaves_the_rfq_byte_identical(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    before = c.get_rfq(rid)
    with pytest.raises(Exception):
        post_bid(vm, c, rid, BOB, PRICE_B, value=BOND - 1)
    with pytest.raises(Exception):
        post_bid(vm, c, rid, BUYER, PRICE_B)
    with pytest.raises(Exception):
        post_bid(vm, c, rid, ALICE, PRICE_B)
    assert c.get_rfq(rid) == before


# ==========================================================================
# C. reveal
# ==========================================================================


def test_reveal_before_bidding_closes_is_refused(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    with pytest.raises(Exception):
        reveal(vm, c, rid, ALICE, PRICE_A, t=BID_CLOSE - 1)
    assert bid_state(vm, c, rid, ALICE)["status"] == "PLACED"


def test_reveal_after_the_window_is_refused(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    with pytest.raises(Exception):
        reveal(vm, c, rid, ALICE, PRICE_A, t=REVEAL_CLOSE)
    assert bid_state(vm, c, rid, ALICE)["status"] == "PLACED"


def test_a_wrong_salt_leaves_the_price_sealed(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A, salt="right")
    before = c.get_bid(rid, ALICE)
    with pytest.raises(Exception):
        reveal(vm, c, rid, ALICE, PRICE_A, salt="wrong")
    assert c.get_bid(rid, ALICE) == before
    assert bid_state(vm, c, rid, ALICE)["status"] == "PLACED"


def test_price_above_the_budget_is_refused(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, BUDGET + 1)
    with pytest.raises(Exception):
        reveal(vm, c, rid, ALICE, BUDGET + 1)
    assert bid_state(vm, c, rid, ALICE)["status"] == "PLACED"


def test_zero_price_is_refused(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, 0)
    with pytest.raises(Exception):
        reveal(vm, c, rid, ALICE, 0)
    assert bid_state(vm, c, rid, ALICE)["status"] == "PLACED"


def test_a_correct_reveal_opens_the_price(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A, salt="s3")
    reveal(vm, c, rid, ALICE, PRICE_A, salt="s3")
    b = bid_state(vm, c, rid, ALICE)
    assert b["status"] == "REVEALED"
    assert b["price"] == str(PRICE_A)


def test_double_reveal_is_refused(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    reveal(vm, c, rid, ALICE, PRICE_A)
    with pytest.raises(Exception):
        reveal(vm, c, rid, ALICE, PRICE_A)
    assert bid_state(vm, c, rid, ALICE)["price"] == str(PRICE_A)


def test_an_address_without_a_bid_cannot_reveal(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    with pytest.raises(Exception):
        reveal(vm, c, rid, STRANGER, PRICE_A)


# ==========================================================================
# D. award
# ==========================================================================


def test_award_before_the_reveal_deadline_is_refused(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    reveal(vm, c, rid, ALICE, PRICE_A)
    with pytest.raises(Exception):
        award(vm, c, rid, t=REVEAL_CLOSE - 1)
    assert state(vm, c, rid)["phase"] == "BIDDING"


def test_ranking_is_cheapest_first(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    three_bidders(vm, c, rid)
    for who, price in ((ALICE, PRICE_A), (BOB, PRICE_B), (CAROL, PRICE_C)):
        reveal(vm, c, rid, who, price)
    assert award(vm, c, rid) == "AWARDED"
    assert [x["bidder"] for x in order(vm, c, rid)] == [ALICE, BOB, CAROL]
    assert [x["price"] for x in order(vm, c, rid)] == [str(PRICE_A), str(PRICE_B), str(PRICE_C)]
    assert state(vm, c, rid)["won_by"] == ALICE


def test_a_tie_is_broken_by_address(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, BOB, PRICE_A)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    reveal(vm, c, rid, BOB, PRICE_A)
    reveal(vm, c, rid, ALICE, PRICE_A)
    award(vm, c, rid)
    assert [x["bidder"] for x in order(vm, c, rid)] == [ALICE, BOB]


def test_no_reveals_refund_the_buyer_and_take_the_no_show_bonds(rfq):
    vm, c = rfq
    before = balance(vm, BUYER)
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    post_bid(vm, c, rid, BOB, PRICE_B)
    assert award(vm, c, rid) == "REFUNDED"
    r = state(vm, c, rid)
    assert r["forfeited"] == str(2 * BOND)
    assert balance(vm, BUYER) == before + 2 * BOND
    assert contract_balance(vm) == 0
    assert bid_state(vm, c, rid, ALICE)["status"] == "FORFEITED"
    assert bid_state(vm, c, rid, BOB)["status"] == "FORFEITED"


def test_a_no_show_forfeits_while_a_revealer_still_holds(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    post_bid(vm, c, rid, BOB, PRICE_B)
    reveal(vm, c, rid, ALICE, PRICE_A)
    award(vm, c, rid)
    r = state(vm, c, rid)
    assert r["forfeited"] == str(BOND)
    assert bid_state(vm, c, rid, BOB)["status"] == "FORFEITED"
    assert bid_state(vm, c, rid, ALICE)["status"] == "REVEALED"
    assert contract_balance(vm) == BUDGET + BOND


def test_award_twice_is_refused(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    reveal(vm, c, rid, ALICE, PRICE_A)
    award(vm, c, rid)
    with pytest.raises(Exception):
        award(vm, c, rid, t=T_AWARD + 5)


# ==========================================================================
# E. delivery
# ==========================================================================


def test_delivery_pays_the_winner_and_returns_the_change(rfq):
    vm, c = rfq
    before = balance(vm, BUYER)
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    reveal(vm, c, rid, ALICE, PRICE_A)
    award(vm, c, rid)
    alice = balance(vm, ALICE)
    assert check(vm, c, rid, T_CHECK) == "SETTLED"
    r = state(vm, c, rid)
    assert r["phase"] == "SETTLED"
    assert r["delivered_by"] == ALICE
    assert r["paid"] == str(PRICE_A)
    assert r["refunded"] == str(BUDGET - PRICE_A)
    assert r["bonds_back"] == str(BOND)
    assert balance(vm, ALICE) == alice + PRICE_A + BOND
    assert balance(vm, BUYER) == before - PRICE_A
    assert contract_balance(vm) == 0


def test_outbid_bonds_come_home_at_settlement(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    three_bidders(vm, c, rid)
    for who, price in ((ALICE, PRICE_A), (BOB, PRICE_B), (CAROL, PRICE_C)):
        reveal(vm, c, rid, who, price)
    award(vm, c, rid)
    bob = balance(vm, BOB)
    carol = balance(vm, CAROL)
    check(vm, c, rid, T_CHECK)
    assert bid_state(vm, c, rid, BOB)["status"] == "OUTBID"
    assert bid_state(vm, c, rid, CAROL)["status"] == "OUTBID"
    assert balance(vm, BOB) == bob + BOND
    assert balance(vm, CAROL) == carol + BOND
    assert state(vm, c, rid)["bonds_back"] == str(3 * BOND)
    assert contract_balance(vm) == 0


def test_the_record_must_name_this_order(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    reveal(vm, c, rid, ALICE, PRICE_A)
    award(vm, c, rid)
    remock(vm, BODY_NO_REF, BODY_NO_REF, span=SPAN_NO_REF)
    assert check(vm, c, rid, T_CHECK) == "NOT_YET"
    r = state(vm, c, rid)
    assert r["phase"] == "AWARDED"
    assert r["reason"] == "the record does not name this order"
    assert r["attempts"] == "1"


def test_another_order_reference_does_not_count(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    reveal(vm, c, rid, ALICE, PRICE_A)
    award(vm, c, rid)
    remock(vm, BODY_OTHER, BODY_OTHER, span="PO-9999: ACME-7 delivered to dock 3.")
    assert check(vm, c, rid, T_CHECK) == "NOT_YET"
    assert state(vm, c, rid)["phase"] == "AWARDED"


def test_the_marker_must_be_inside_the_quote(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    reveal(vm, c, rid, ALICE, PRICE_A)
    award(vm, c, rid)
    remock(vm, BODY_OK, BODY_OK, span="PO-4711: delivered to dock 3.")
    assert check(vm, c, rid, T_CHECK) == "NOT_YET"


def test_the_quote_must_really_be_in_the_body(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    reveal(vm, c, rid, ALICE, PRICE_A)
    award(vm, c, rid)
    remock(vm, BODY_OK, BODY_OK, span="PO-4711: ACME-7 delivered to dock 9.")
    assert check(vm, c, rid, T_CHECK) == "NOT_YET"


def test_an_empty_quote_is_never_enough(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    reveal(vm, c, rid, ALICE, PRICE_A)
    award(vm, c, rid)
    remock(vm, BODY_OK, BODY_OK, span="")
    assert check(vm, c, rid, T_CHECK) == "NOT_YET"


def test_one_unreachable_source_fails_closed(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    reveal(vm, c, rid, ALICE, PRICE_A)
    award(vm, c, rid)
    remock(vm, BODY_EMPTY, BODY_OK)
    assert check(vm, c, rid, T_CHECK) == "NOT_YET"
    assert state(vm, c, rid)["reason"] == "a source was unreachable"


def test_one_source_without_the_record_is_not_enough(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    reveal(vm, c, rid, ALICE, PRICE_A)
    award(vm, c, rid)
    remock(vm, BODY_OK, BODY_PLAN)
    assert check(vm, c, rid, T_CHECK) == "NOT_YET"
    assert state(vm, c, rid)["phase"] == "AWARDED"


def test_attempts_accumulate_while_the_window_is_open(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    reveal(vm, c, rid, ALICE, PRICE_A)
    award(vm, c, rid)
    remock(vm, BODY_PLAN, BODY_PLAN)
    check(vm, c, rid, T_CHECK)
    check(vm, c, rid, T_CHECK + 10)
    r = state(vm, c, rid)
    assert r["attempts"] == "2"
    assert r["phase"] == "AWARDED"
    assert r["won_by"] == ALICE


def test_check_before_the_award_is_refused(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    reveal(vm, c, rid, ALICE, PRICE_A)
    with pytest.raises(Exception):
        check(vm, c, rid, T_CHECK)


def test_check_after_settlement_is_refused(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    reveal(vm, c, rid, ALICE, PRICE_A)
    award(vm, c, rid)
    check(vm, c, rid, T_CHECK)
    with pytest.raises(Exception):
        check(vm, c, rid, T_CHECK + 1)


def test_the_spans_are_kept_for_the_reviewer(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    reveal(vm, c, rid, ALICE, PRICE_A)
    award(vm, c, rid)
    check(vm, c, rid, T_CHECK)
    assert state(vm, c, rid)["spans"] == [SPAN_OK, SPAN_OK]


# ==========================================================================
# F. cascade
# ==========================================================================


def test_a_missed_window_hands_the_job_to_the_next_bid(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    three_bidders(vm, c, rid)
    for who, price in ((ALICE, PRICE_A), (BOB, PRICE_B), (CAROL, PRICE_C)):
        reveal(vm, c, rid, who, price)
    award(vm, c, rid)
    alice = balance(vm, ALICE)
    bob = balance(vm, BOB)
    remock(vm, BODY_PLAN, BODY_PLAN)
    assert check(vm, c, rid, T_LATE) == "REAWARDED"
    r = state(vm, c, rid)
    assert r["won_by"] == BOB
    assert r["passed_on"] == str(BOND)
    assert bid_state(vm, c, rid, ALICE)["status"] == "MISSED"
    assert balance(vm, ALICE) == alice
    assert balance(vm, BOB) == bob + BOND
    assert r["phase"] == "AWARDED"


def test_the_next_bidder_can_still_deliver(rfq):
    vm, c = rfq
    before = balance(vm, BUYER)
    rid = open_rfq(vm, c)
    three_bidders(vm, c, rid)
    for who, price in ((ALICE, PRICE_A), (BOB, PRICE_B), (CAROL, PRICE_C)):
        reveal(vm, c, rid, who, price)
    award(vm, c, rid)
    remock(vm, BODY_PLAN, BODY_PLAN)
    check(vm, c, rid, T_LATE)
    remock(vm, BODY_OK, BODY_OK)
    bob = balance(vm, BOB)
    assert check(vm, c, rid, T_LATE + 1) == "SETTLED"
    r = state(vm, c, rid)
    assert r["delivered_by"] == BOB
    assert r["paid"] == str(PRICE_B)
    assert r["refunded"] == str(BUDGET - PRICE_B)
    # Bob keeps his price plus his own bond plus the bond Alice forfeited.
    assert balance(vm, BOB) == bob + PRICE_B + BOND
    assert balance(vm, BUYER) == before - PRICE_B
    assert contract_balance(vm) == 0
    assert int(r["paid"]) + int(r["refunded"]) == BUDGET
    assert int(r["forfeited"]) + int(r["passed_on"]) + int(r["bonds_back"]) == 3 * BOND


def test_the_cascade_skips_a_bidder_who_already_missed(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    three_bidders(vm, c, rid)
    for who, price in ((ALICE, PRICE_A), (BOB, PRICE_B), (CAROL, PRICE_C)):
        reveal(vm, c, rid, who, price)
    award(vm, c, rid)
    remock(vm, BODY_PLAN, BODY_PLAN)
    check(vm, c, rid, T_LATE)
    assert state(vm, c, rid)["won_by"] == BOB
    assert check(vm, c, rid, T_LATE + WINDOW + 1) == "REAWARDED"
    r = state(vm, c, rid)
    assert r["won_by"] == CAROL
    assert bid_state(vm, c, rid, BOB)["status"] == "MISSED"
    assert bid_state(vm, c, rid, ALICE)["status"] == "MISSED"
    assert r["passed_on"] == str(2 * BOND)


def test_a_failed_delivery_never_pays_the_buyer(rfq):
    vm, c = rfq
    before = balance(vm, BUYER)
    rid = open_rfq(vm, c)
    three_bidders(vm, c, rid)
    for who, price in ((ALICE, PRICE_A), (BOB, PRICE_B), (CAROL, PRICE_C)):
        reveal(vm, c, rid, who, price)
    award(vm, c, rid)
    remock(vm, BODY_PLAN, BODY_PLAN)
    check(vm, c, rid, T_LATE)
    check(vm, c, rid, T_LATE + WINDOW + 1)
    check(vm, c, rid, T_LATE + 2 * WINDOW + 2)
    assert balance(vm, BUYER) == before
    assert state(vm, c, rid)["phase"] == "REFUNDED"


def test_when_everyone_misses_the_buyer_gets_the_budget_back(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    reveal(vm, c, rid, ALICE, PRICE_A)
    award(vm, c, rid)
    remock(vm, BODY_PLAN, BODY_PLAN)
    alice = balance(vm, ALICE)
    assert check(vm, c, rid, T_LATE) == "REFUNDED"
    r = state(vm, c, rid)
    assert r["phase"] == "REFUNDED"
    assert r["refunded"] == str(BUDGET)
    assert r["paid"] == "0"
    assert r["bonds_back"] == str(BOND)
    assert balance(vm, ALICE) == alice + BOND
    assert contract_balance(vm) == 0


# ==========================================================================
# G. cancel
# ==========================================================================


def test_the_buyer_can_cancel_before_bidding_closes(rfq):
    vm, c = rfq
    before = balance(vm, BUYER)
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    post_bid(vm, c, rid, BOB, PRICE_B)
    alice = balance(vm, ALICE)
    bob = balance(vm, BOB)
    vm.warp(__import__("conftest").iso(EPOCH0 + 60))
    vm.sender = BUYER
    c.cancel(rid)
    r = state(vm, c, rid)
    assert r["phase"] == "CANCELLED"
    assert r["refunded"] == str(BUDGET)
    assert r["bonds_back"] == str(2 * BOND)
    assert balance(vm, BUYER) == before
    assert balance(vm, ALICE) == alice + BOND
    assert balance(vm, BOB) == bob + BOND
    assert contract_balance(vm) == 0


def test_a_stranger_cannot_cancel(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    vm.warp(__import__("conftest").iso(EPOCH0 + 60))
    vm.sender = STRANGER
    with pytest.raises(Exception):
        c.cancel(rid)
    assert state(vm, c, rid)["phase"] == "BIDDING"


def test_the_buyer_cannot_cancel_once_bidding_closed(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    vm.warp(__import__("conftest").iso(BID_CLOSE))
    vm.sender = BUYER
    with pytest.raises(Exception):
        c.cancel(rid)
    assert state(vm, c, rid)["phase"] == "BIDDING"


def test_the_buyer_can_reclaim_a_dead_rfq_with_no_bids(rfq):
    vm, c = rfq
    before = balance(vm, BUYER)
    rid = open_rfq(vm, c)
    vm.warp(__import__("conftest").iso(BID_CLOSE + 1))
    vm.sender = BUYER
    c.cancel(rid)
    assert state(vm, c, rid)["phase"] == "CANCELLED"
    assert balance(vm, BUYER) == before
    assert contract_balance(vm) == 0


def test_cancel_after_the_award_is_refused(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    reveal(vm, c, rid, ALICE, PRICE_A)
    award(vm, c, rid)
    vm.sender = BUYER
    with pytest.raises(Exception):
        c.cancel(rid)


# ==========================================================================
# H. invariants and views
# ==========================================================================


def test_every_terminal_path_leaves_no_unit_behind(rfq):
    vm, c = rfq
    # cancelled
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    vm.warp(__import__("conftest").iso(EPOCH0 + 60))
    vm.sender = BUYER
    c.cancel(rid)
    assert contract_balance(vm) == 0
    # settled
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    reveal(vm, c, rid, ALICE, PRICE_A)
    award(vm, c, rid)
    check(vm, c, rid, T_CHECK)
    assert contract_balance(vm) == 0
    # refunded after every bidder missed
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    reveal(vm, c, rid, ALICE, PRICE_A)
    award(vm, c, rid)
    remock(vm, BODY_PLAN, BODY_PLAN)
    check(vm, c, rid, T_LATE)
    assert contract_balance(vm) == 0


def test_views_return_json_strings_with_string_amounts(rfq):
    vm, c = rfq
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    reveal(vm, c, rid, ALICE, PRICE_A)
    award(vm, c, rid)
    a = c.get_rfq(rid)
    b = c.get_bid(rid, ALICE)
    o = c.get_order(rid)
    assert isinstance(a, str) and isinstance(b, str) and isinstance(o, str)
    assert json.loads(a)["budget"] == str(BUDGET)
    assert json.loads(b)["bond"] == str(BOND)
    assert json.loads(o)["order"][0]["price"] == str(PRICE_A)


def test_an_unknown_rfq_is_refused(rfq):
    vm, c = rfq
    with pytest.raises(Exception):
        c.get_rfq("999")
    with pytest.raises(Exception):
        check(vm, c, "999", T_CHECK)


def test_a_record_for_one_order_cannot_settle_another(rfq):
    vm, c = rfq
    first = open_rfq(vm, c, ref="PO-4711")
    second = open_rfq(vm, c, ref="PO-9999")
    for rid in (first, second):
        post_bid(vm, c, rid, ALICE, PRICE_A)
        reveal(vm, c, rid, ALICE, PRICE_A)
        award(vm, c, rid)
    assert check(vm, c, first, T_CHECK) == "SETTLED"
    assert check(vm, c, second, T_CHECK) == "NOT_YET"
    assert state(vm, c, second)["phase"] == "AWARDED"


def test_two_rfqs_keep_separate_books(rfq):
    vm, c = rfq
    a = open_rfq(vm, c, budget=BUDGET)
    b = open_rfq(vm, c, budget=BUDGET // 2)
    post_bid(vm, c, a, ALICE, PRICE_A)
    post_bid(vm, c, b, BOB, PRICE_A // 2)
    reveal(vm, c, a, ALICE, PRICE_A)
    reveal(vm, c, b, BOB, PRICE_A // 2)
    award(vm, c, a)
    assert state(vm, c, a)["phase"] == "AWARDED"
    assert state(vm, c, b)["phase"] == "BIDDING"
    award(vm, c, b)
    check(vm, c, a, T_CHECK)
    assert state(vm, c, a)["phase"] == "SETTLED"
    assert state(vm, c, b)["phase"] == "AWARDED"
    assert contract_balance(vm) == BUDGET // 2 + BOND


def test_conservation_holds_with_a_no_show_and_a_delivery(rfq):
    vm, c = rfq
    before = balance(vm, BUYER)
    rid = open_rfq(vm, c)
    post_bid(vm, c, rid, ALICE, PRICE_A)
    post_bid(vm, c, rid, BOB, PRICE_B)
    post_bid(vm, c, rid, CAROL, PRICE_C)
    reveal(vm, c, rid, ALICE, PRICE_A)
    reveal(vm, c, rid, BOB, PRICE_B)
    award(vm, c, rid)
    check(vm, c, rid, T_CHECK)
    r = state(vm, c, rid)
    assert int(r["paid"]) + int(r["refunded"]) == BUDGET
    assert int(r["forfeited"]) + int(r["passed_on"]) + int(r["bonds_back"]) == 3 * BOND
    assert r["forfeited"] == str(BOND)
    assert balance(vm, BUYER) == before - PRICE_A + BOND
    assert contract_balance(vm) == 0
