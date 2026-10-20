# Sealed RFQ

A procurement escrow for work bought from an open market.

A buyer escrows a budget and, **once**, names three things: an order reference,
a completion marker, and **two sources on different hosts**. Bidders post a
bond and a **sealed** price. After the bid window the prices are opened, the
cheapest reveal takes the job, and delivery is checked against those two fixed
sources — never against anything the bidder or the buyer can add later.

If the winner does not prove delivery inside its window, the job **cascades to
the next cheapest bidder**, with a fresh window. When nobody is left, the
buyer gets the budget back. Every wei ends in exactly one place.

This is a primitive, not an app: 6 write methods, 4 views, no frontend.

---

## Why this exists

Buying work from strangers needs three answers: who sets the price, who says it
was delivered, and what happens when it wasn't. On a normal chain the first is
an auction, the second is an oracle, and the third is a multisig. Each one
imports a trusted party.

GenLayer can read the web under consensus, so the second answer can be
**deterministic given a fixed record**, and the first and third are arithmetic.
The result is a purchase order that needs nobody's signature at the end: the
buyer's money and the bidder's labour change hands on evidence, not on
permission.

---

## Mechanism

| Method | Who | What it does |
|---|---|---|
| `open_rfq` | buyer, payable | escrows the budget; freezes reference, marker, two sources, deadlines, window, bond |
| `bid` | any address except the buyer, payable | posts exactly the bond and a **sha256 commitment** to a price |
| `reveal` | the bidder | opens the price inside the reveal window; a wrong salt or an over-budget price leaves the bid sealed |
| `open_award` | anyone | ranks revealed prices (cheapest first, ties by address), forfeits no-show bonds, names the winner. **No model call** |
| `check_delivery` | anyone | fetches the two sources; pays on proof, waits while the window is open, otherwise hands the job down the queue |
| `cancel` | buyer | refunds everything, but **only before any reveal can exist** |

State: `BIDDING → AWARDED → SETTLED` or `REFUNDED`. `cancel` is the only exit
from `BIDDING`, and it closes when bidding does (a buyer may still reclaim a
dead RFQ that received no bids).

Bid statuses: `PLACED → REVEALED → DELIVERED | MISSED | OUTBID`, or `FORFEITED`
for a commitment that was never opened.

The commitment preimage is exactly `"<price>|<salt>"`, hashed with sha256 and
hex-encoded — the same bytes the contract checks in `reveal`.

---

## What the model does, and what it cannot do

The only non-deterministic step is inside `check_delivery`, and it is narrow:
per source, consensus returns **one sentence** that contains the marker and
reports the order done. Everything that decides money is checked afterwards, in
code:

1. the quote is non-empty;
2. the quote actually occurs in the body the contract fetched;
3. the quote contains the marker;
4. **the body names this order reference** — so a record that proves *some*
   delivery cannot be replayed against a different purchase order.

Both sources must pass (all-of), and they must be different hosts, checked at
creation. A model can therefore only **lose** credit: an invented or embellished
quote is not in the body, and an empty quote never counts (`"" in anything` is
true, so emptiness is rejected before any substring test).

Fail-closed: an unreachable source, an empty body, or a missing record yields
`NOT_YET`, never a payment.

---

## Why this is not another "fetch a URL and pay" contract

| | decision | lifecycle | who fixes the evidence |
|---|---|---|---|
| spec-bounty | rules over submitted values | collect → submit → settle | funder names sources |
| delivery-graph | DAG gating (a failed parent blocks children) | ordered dependencies | recipient may file URLs |
| checkpoint-retainer | per-tranche two-source proof | independent time slices | payer, two URLs, no filing entry |
| **sealed-rfq** | **price ranking is arithmetic; delivery is a two-source check** | **the job moves down a queue of bidders** | **buyer, two URLs, no filing entry** |

The distinctive part is the **queue**: the winner is not replaced by a vote or
by a new auction, but by the next price in the same sealed book, and the bond
travels with the job. Commit-then-reveal is borrowed from the bonded audit
committee — it is the standard way to keep a bid private — but nothing else is:
there is no jury, no majority, and no slash vote.

---

## The three questions that killed earlier submissions

| question | answer here |
|---|---|
| Who decides **what** is judged? | the buyer, once, when the RFQ is created |
| Who decides **where** to look? | the buyer, once: two URLs on two hosts |
| Who **votes**? | nobody. Prices are compared arithmetically; the model only quotes a sentence |

There is **no filing entry at all**. `bid` and `reveal` accept no evidence, and
`check_delivery` takes only the RFQ id. That removes the two failure modes that
have cost this account submissions before:

- a capacity-limited filing slot cannot be flooded, because there is no slot;
- a bidder cannot narrow or redefine the judged set, because it is frozen before
  any bid exists.

The remaining ownership rule: **the buyer may not bid** on its own RFQ, and
**may not cancel once bidding has closed** (with bids). Both are enforced, and
both are unnecessary to trust the rest of the design.

---

## Money, and why the bond follows the queue

- The **budget** is escrowed at creation and leaves exactly twice at most: to
  the winning bidder as `price`, and to the buyer as `budget - price`.
- Each **bond** is escrowed with the bid and leaves exactly once: back to the
  bidder, forfeited to the buyer (only for a commitment that was never opened),
  or passed to the next bidder in the queue (when the winner misses its window).
- The buyer never receives a bond for a *failed delivery*, because the buyer also
  chooses the record. If the bond paid the buyer on failure, a buyer could name
  a record that can never come true and collect every bond. Instead the failed
  winner's bond goes to the next bidder, so a buyer gains nothing from failure
  while a bidder still pays for wasting the queue.

Accounting invariants, both asserted in tests:

```
paid + refunded            == budget          at any terminal state
forfeited + passed_on + bonds_back
                           == bond * bidders  at any terminal state
contract balance           == 0               at any terminal state
```

`open_award` is permissionless and callable by anyone, and `check_delivery` is
too, so a winner can always trigger the payment that is owed to it.

A bidder that wins and then abandons loses the bond to the next bidder and its
own price; that is a bounded cost to the buyer (one `window`), not an unbounded
one.

---

## Trust boundaries

Stated plainly rather than hidden:

1. **The buyer writes the record.** A buyer who names a record that can never
   come true can withhold its own escrow, and every winner's bond goes to the
   next bidder — never to the buyer. Bidders can read the record and the two
   sources before posting a bond, and the bond is deliberately small.
2. **Two hosts are not two hands.** The sources are public documents. If the
   winner can edit them, it can manufacture its own proof: the contract makes
   the venue explicit, immutable and checkable, but it cannot make a wiki
   trustworthy. A buyer should name venues the winner cannot write to.
3. **A reveal that never happens forfeits the bond to the buyer.** The buyer
   cannot cause that — the salt is the bidder's — and the reveal window is
   public before bidding, so a bidder can decline the terms.
4. **The model is a reader, not a judge.** It can suppress a true delivery
   (say the quote is subtly wrong) and cost the queue a window; it cannot
   manufacture a payment, because the quote must occur verbatim in a body the
   code fetched and the body must name the order.

---

## Tests

76 direct-mode tests in 8 groups:

| group | what it pins |
|---|---|
| A creation | budget held whole, terms frozen, sources on two hosts, impossible schedules refused, refusals leave the count at zero |
| B bidding | exact bond, buyer excluded, one bid per address, deadline, full board, 64-hex commitments, **refusal leaves the RFQ byte-identical** |
| C reveal | window edges, wrong salt leaves the price sealed, over-budget and zero prices refused, double reveal refused |
| D award | cheapest first, ties by address, no-show forfeiture, award twice refused |
| E delivery | payment and change, order reference required, marker inside the quote, quote inside the body, empty quote rejected, one unreachable source fails closed, outbid bonds returned |
| F cascade | a missed window moves the job to the next price, a second failure moves it again, **the buyer is never paid by a failure**, exhaustion refunds the budget |
| G cancel | buyer only, impossible after bidding closes, allowed for a dead RFQ with no bids |
| H invariants | every terminal path leaves zero in the contract, two RFQs keep separate books, a record for one order cannot settle another, views are JSON strings |

Fund movement is asserted against real balances, not against calls: the fixture
books payable value to the contract and `emit_transfer` to its recipient, and
**restores the value when a payable call is refused**, so "a rejection moved
nothing" is a real assertion.

---

## Live on chain

Deployed on Bradbury at `0xE936df8C8Ad6B7d048281FC74eA87983DB0B5a98`.

The delivery log used by the live run is in this repository, served from two
hosts:

```
raw.githubusercontent.com/qlxjcj/genlayer-sealed-rfq/master/fixtures/delivery-log.txt
cdn.jsdelivr.net/gh/qlxjcj/genlayer-sealed-rfq@master/fixtures/delivery-log.txt
```

It contains two lines:

```
Order PO-4711: marker ACME-7 delivered on 2026-10-20.
Order PO-9001: marker ACME-9 planned for November, not shipped yet.
```

so one purchase order is genuinely delivered and the other is not — the two
cases the contract has to tell apart.

## Source integrity

```
sha256  78283440bf72a9db285e20bea8c72a97b0697b2805cd71511fc68f36f5ea5011
bytes   19991
```

Reproduce with `genlayer code <address> --rpc https://rpc-bradbury.genlayer.com`
and compare against `sealed_rfq.py`.

---

## Layout

```
sealed_rfq.py                 the contract
tests/direct/conftest.py      balance hooks + time helpers
tests/direct/test_sealed_rfq.py
fixtures/delivery-log.txt     the record the live demo reads
```

## Reproduce

```bash
genvm-lint check sealed_rfq.py --json
python -m pytest tests/direct -q
genlayer deploy --contract sealed_rfq.py --rpc https://rpc-bradbury.genlayer.com
```
