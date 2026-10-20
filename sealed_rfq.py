# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
import hashlib
import json
from genlayer import *

MAX_BIDDERS = 16
MAX_REF = 40
MAX_MARKER = 80
MIN_BOND = 10 ** 14
MIN_WINDOW = 60

BIDDING = "BIDDING"
AWARDED = "AWARDED"
SETTLED = "SETTLED"
REFUNDED = "REFUNDED"
CANCELLED = "CANCELLED"

PLACED = "PLACED"
REVEALED = "REVEALED"
FORFEITED = "FORFEITED"
MISSED = "MISSED"
DELIVERED = "DELIVERED"
OUTBID = "OUTBID"

SPACE = " \t\r\n\f\v"


class SealedRFQ(gl.Contract):
    """A buyer escrows a budget and freezes once the order reference, the marker
    and two independent sources. Bidders seal a price; the cheapest reveal wins,
    and a winner who misses the window hands the job down the queue. No filing
    entry, no jury, and the buyer cannot bid."""

    rfqs: TreeMap[str, str]
    bids: TreeMap[str, str]
    rfq_count: u256

    def __init__(self):
        self.rfq_count = 0

    def _addr_hex(self, a) -> str:
        if isinstance(a, str):
            return a
        h = getattr(a, "as_hex", None)
        if isinstance(h, str):
            return h
        if isinstance(a, (bytes, bytearray)):
            return "0x" + bytes(a).hex()
        return str(a)

    def _iso_to_epoch(self, s: str) -> int:
        s = s.strip()
        if len(s) < 19:
            return 0
        offset = 0
        tail = s[19:]
        if tail.endswith("Z"):
            tail = tail[:-1]
        if tail and (tail[0] == "+" or tail[0] == "-"):
            sign = 1 if tail[0] == "+" else -1
            rest = tail[1:5]
            hh = int(rest[0:2]) if rest[0:2].isdigit() else 0
            mm = int(rest[2:4]) if rest[2:4].isdigit() else 0
            offset = sign * (hh * 3600 + mm * 60)
        days = 0
        y = int(s[0:4])
        mo = int(s[5:7])
        d = int(s[8:10])
        for yr in range(1970, y):
            days += 366 if (yr % 4 == 0 and (yr % 100 != 0 or yr % 400 == 0)) else 365
        cum = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
        days += cum[mo - 1]
        if mo > 2 and (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)):
            days += 1
        days += d - 1
        hh = int(s[11:13]) if s[11:13].isdigit() else 0
        mi = int(s[14:16]) if s[14:16].isdigit() else 0
        ss = int(s[17:19]) if s[17:19].isdigit() else 0
        return days * 86400 + hh * 3600 + mi * 60 + ss - offset

    def _now(self) -> int:
        raw = getattr(gl, "message_raw", None)
        d = None
        if raw is not None:
            try:
                d = raw["datetime"]
            except Exception:
                d = None
        if d is None:
            return 0
        return self._iso_to_epoch(d if isinstance(d, str) else str(d))

    def _squash(self, text: str) -> str:
        out = []
        for ch in str(text).lower():
            if ch in SPACE:
                continue
            out.append(ch)
        return "".join(out)

    def _host(self, url: str) -> str:
        u = str(url).strip().lower()
        i = u.find("://")
        if i >= 0:
            u = u[i + 3:]
        for ch in "/?#":
            j = u.find(ch)
            if j >= 0:
                u = u[:j]
        k = u.rfind("@")
        if k >= 0:
            u = u[k + 1:]
        c = u.rfind(":")
        if c >= 0:
            u = u[:c]
        return u

    def _decode_body(self, content) -> str:
        body = getattr(content, "body", None)
        if body is None:
            return str(content)
        return body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)

    def _transfer(self, recipient, amount: int):
        if amount > 0:
            gl.get_contract_at(Address(recipient)).emit_transfer(value=u256(amount))

    def _bkey(self, rfq_id: str, bidder: str) -> str:
        return str(rfq_id) + "|" + str(bidder).lower()

    def _load(self, rfq_id: str) -> dict:
        raw = self.rfqs.get(str(rfq_id), "")
        if not raw:
            raise gl.vm.UserError("No such RFQ")
        return json.loads(raw)

    def _save(self, r: dict):
        self.rfqs[str(r["rfq_id"])] = json.dumps(r)

    def _load_bid(self, rfq_id: str, bidder: str) -> dict:
        raw = self.bids.get(self._bkey(rfq_id, bidder), "")
        if not raw:
            raise gl.vm.UserError("No bid from that address")
        return json.loads(raw)

    def _save_bid(self, rfq_id: str, bid: dict):
        self.bids[self._bkey(rfq_id, bid["bidder"])] = json.dumps(bid)

    def _hash(self, price, salt) -> str:
        return hashlib.sha256((str(int(price)) + "|" + str(salt)).encode("utf-8")).hexdigest()

    def _examine(self, sources: list, marker: str, ref: str) -> dict:
        """Two buyer-fixed sources; the verdict is computed in code, in the block."""

        def check() -> dict:
            bodies = []
            for u in sources:
                try:
                    bodies.append(self._decode_body(gl.nondet.web.render(str(u))))
                except Exception:
                    bodies.append("")
            if not bodies[0] or not bodies[1]:
                return {"retrieved": False, "tagged": False, "ok": False, "spans": ["", ""]}
            want_ref = self._squash(ref)
            tagged = want_ref in self._squash(bodies[0]) and want_ref in self._squash(bodies[1])
            spans = []
            for b in bodies:
                task = ("A purchase order is complete when a delivery record says so. Return "
                        "the ONE sentence that contains MARKER and reports the order done "
                        "(delivered / shipped / completed / fulfilled), copied exactly. If no "
                        "such sentence exists, return an empty string.\n\nMARKER: " + marker +
                        "\nRECORD:\n" + b[:900] + '\n\nJSON only: {"span": "<sentence>"}')
                r = gl.nondet.exec_prompt(task)
                if isinstance(r, str):
                    r = json.loads(r.replace("```json", "").replace("```", ""))
                spans.append(str(r.get("span", "")) if isinstance(r, dict) else "")
            ok = tagged and bool(spans[0]) and bool(spans[1])
            want = self._squash(marker)
            for i in (0, 1):
                if not ok:
                    break
                if not want or want not in self._squash(spans[i]):
                    ok = False
                elif self._squash(spans[i]) not in self._squash(bodies[i]):
                    ok = False
            return {"retrieved": True, "tagged": tagged, "ok": ok, "spans": [spans[0], spans[1]]}

        principle = ("Equivalent only if retrieved, tagged and ok agree exactly; the span is an "
                     "audit trail, but empty stays empty.")
        return gl.eq_principle.prompt_comparative(check, principle)

    @gl.public.write.payable
    def open_rfq(self, order_ref: str, marker: str, sources: list, bid_until: int,
                 reveal_until: int, window: int, bond: int) -> str:
        """Escrow the budget and freeze what will be judged."""
        buyer = self._addr_hex(gl.message.sender_address)
        budget = int(gl.message.value)
        if budget <= 0:
            raise gl.vm.UserError("Budget must be > 0")
        ref = str(order_ref).strip()
        if not ref or len(ref) > MAX_REF:
            raise gl.vm.UserError("Order reference required")
        mk = str(marker).strip()
        if not mk or len(mk) > MAX_MARKER:
            raise gl.vm.UserError("Completion marker required")
        if not isinstance(sources, list) or len(sources) != 2:
            raise gl.vm.UserError("Exactly two sources are required")
        urls = []
        for u in sources:
            if not isinstance(u, str):
                raise gl.vm.UserError("Sources must be http(s)")
            u = u.strip()
            if not (u.startswith("http://") or u.startswith("https://")):
                raise gl.vm.UserError("Sources must be http(s)")
            urls.append(u)
        h0 = self._host(urls[0])
        h1 = self._host(urls[1])
        if not h0 or not h1 or h0 == h1:
            raise gl.vm.UserError("Sources must be on different hosts")
        now = self._now()
        try:
            close = int(bid_until)
            reveal = int(reveal_until)
            win = int(window)
            bnd = int(bond)
        except Exception:
            raise gl.vm.UserError("Deadlines, window and bond must be integers")
        if close <= now:
            raise gl.vm.UserError("Bidding must close in the future")
        if reveal <= close:
            raise gl.vm.UserError("Reveal must close after bidding")
        if win < MIN_WINDOW:
            raise gl.vm.UserError("Delivery window must be at least " + str(MIN_WINDOW))
        if bnd < MIN_BOND:
            raise gl.vm.UserError("Bond must be at least " + str(MIN_BOND))

        self.rfq_count += 1
        rid = str(int(self.rfq_count))
        self.rfqs[rid] = json.dumps({
            "rfq_id": rid, "buyer": buyer, "order_ref": ref, "marker": mk,
            "sources": urls, "bond": str(bnd), "budget": str(budget),
            "bid_until": str(close), "reveal_until": str(reveal), "window": str(win),
            "phase": BIDDING, "bidders": "0", "roster": [],
            "order": [], "award_seq": "-1", "won_by": "", "awarded_at": "0",
            "attempts": "0", "paid": "0", "refunded": "0", "forfeited": "0",
            "passed_on": "0", "bonds_back": "0", "forfeit_pool": "0",
            "delivered_by": "", "reason": "", "spans": ["", ""],
        })
        return rid

    @gl.public.write.payable
    def bid(self, rfq_id: str, commitment: str):
        """Post the hash of a price, never the price."""
        r = self._load(rfq_id)
        if str(r["phase"]) != BIDDING:
            raise gl.vm.UserError("RFQ is " + str(r["phase"]))
        if self._now() >= int(r["bid_until"]):
            raise gl.vm.UserError("Bidding has closed")
        sender = self._addr_hex(gl.message.sender_address)
        if sender.lower() == str(r["buyer"]).lower():
            raise gl.vm.UserError("The buyer may not bid on its own RFQ")
        if int(gl.message.value) != int(r["bond"]):
            raise gl.vm.UserError("Bid must post exactly the bond")
        if not isinstance(commitment, str) or len(commitment) != 64:
            raise gl.vm.UserError("Commitment must be 64 hex chars")
        for ch in commitment.lower():
            if ch not in "0123456789abcdef":
                raise gl.vm.UserError("Commitment must be hex")
        if self.bids.get(self._bkey(rfq_id, sender), ""):
            raise gl.vm.UserError("One bid per address")
        if int(r["bidders"]) >= MAX_BIDDERS:
            raise gl.vm.UserError("Bid board is full")
        self._save_bid(rfq_id, {"bidder": sender, "commitment": commitment,
                                "bond": str(int(gl.message.value)), "price": "0",
                                "status": PLACED, "reason": ""})
        r["roster"] = [str(x) for x in r["roster"]] + [sender]
        r["bidders"] = str(int(r["bidders"]) + 1)
        self._save(r)

    @gl.public.write
    def reveal(self, rfq_id: str, price: int, salt: str):
        """Open the price; a wrong salt leaves the bid sealed and forfeits it."""
        r = self._load(rfq_id)
        if str(r["phase"]) != BIDDING:
            raise gl.vm.UserError("RFQ is " + str(r["phase"]))
        now = self._now()
        if now < int(r["bid_until"]):
            raise gl.vm.UserError("Bidding is still open")
        if now >= int(r["reveal_until"]):
            raise gl.vm.UserError("Reveal window has closed")
        b = self._load_bid(rfq_id, self._addr_hex(gl.message.sender_address))
        if str(b["status"]) != PLACED:
            raise gl.vm.UserError("Bid is already " + str(b["status"]))
        p = int(price)
        if p <= 0:
            raise gl.vm.UserError("Price must be > 0")
        if p > int(r["budget"]):
            raise gl.vm.UserError("Price is above the escrowed budget")
        if self._hash(p, salt) != str(b["commitment"]):
            raise gl.vm.UserError("Reveal does not match the commitment")
        b["price"] = str(p)
        b["status"] = REVEALED
        self._save_bid(rfq_id, b)

    @gl.public.write
    def open_award(self, rfq_id: str) -> str:
        """Rank prices, take the cheapest, forfeit no-shows. No model."""
        r = self._load(rfq_id)
        if str(r["phase"]) != BIDDING:
            raise gl.vm.UserError("RFQ is " + str(r["phase"]))
        now = self._now()
        if now < int(r["reveal_until"]):
            raise gl.vm.UserError("Reveal window is still open")
        pool = 0
        order = []
        for addr in r["roster"]:
            b = self._load_bid(rfq_id, str(addr))
            if str(b["status"]) == REVEALED:
                order.append([int(b["price"]), str(b["bidder"]).lower()])
            elif str(b["status"]) == PLACED:
                b["status"] = FORFEITED
                b["reason"] = "committed but never revealed"
                self._save_bid(rfq_id, b)
                pool += int(b["bond"])
        order.sort()
        r["order"] = [x[1] for x in order]
        r["forfeited"] = str(int(r["forfeited"]) + pool)
        if not order:
            r["phase"] = REFUNDED
            r["refunded"] = str(int(r["budget"]))
            r["reason"] = "no price was revealed"
            self._save(r)
            self._transfer(r["buyer"], int(r["budget"]) + pool)
            return REFUNDED
        r["phase"] = AWARDED
        r["award_seq"] = "0"
        r["won_by"] = order[0][1]
        r["awarded_at"] = str(now)
        r["attempts"] = "0"
        self._save(r)
        if pool:
            self._transfer(r["buyer"], pool)
        return AWARDED

    @gl.public.write
    def check_delivery(self, rfq_id: str) -> str:
        """Proven -> pay; otherwise wait, then hand the job down the queue."""
        r = self._load(rfq_id)
        if str(r["phase"]) != AWARDED:
            raise gl.vm.UserError("RFQ is " + str(r["phase"]))
        win = str(r["won_by"])
        b = self._load_bid(rfq_id, win)
        now = self._now()
        r["attempts"] = str(int(r["attempts"]) + 1)

        res = self._examine(r["sources"], str(r["marker"]), str(r["order_ref"]))
        if not (isinstance(res, dict) and isinstance(res.get("ok"), bool)):
            raise gl.vm.UserError("Invalid examination result")
        spans = res.get("spans")
        if not isinstance(spans, list) or len(spans) != 2:
            spans = ["", ""]

        if res.get("ok") is True:
            price = int(b["price"])
            b["status"] = DELIVERED
            b["reason"] = "both sources name this order and report it complete"
            self._save_bid(rfq_id, b)
            r["phase"] = SETTLED
            r["paid"] = str(price)
            r["refunded"] = str(int(r["budget"]) - price)
            r["delivered_by"] = win
            r["spans"] = [str(spans[0]), str(spans[1])]
            r["reason"] = "delivered"
            back = int(b["bond"])
            loser = []
            for addr in r["order"]:
                if str(addr).lower() == win.lower():
                    continue
                ob = self._load_bid(rfq_id, str(addr))
                if str(ob["status"]) == REVEALED:
                    ob["status"] = OUTBID
                    ob["reason"] = "outbid; bond returned"
                    self._save_bid(rfq_id, ob)
                    back += int(ob["bond"])
                    loser.append(str(addr))
            r["bonds_back"] = str(int(r["bonds_back"]) + back)
            self._save(r)
            self._transfer(win, price + int(b["bond"]))
            self._transfer(r["buyer"], int(r["budget"]) - price)
            for addr in loser:
                self._transfer(addr, int(self._load_bid(rfq_id, addr)["bond"]))
            return SETTLED

        if now < int(r["awarded_at"]) + int(r["window"]):
            r["reason"] = ("a source was unreachable" if not res.get("retrieved")
                           else ("the record does not name this order" if not res.get("tagged")
                                 else "not reported complete by both sources"))
            self._save(r)
            return "NOT_YET"

        # The window is gone. The bond follows the queue, so a buyer naming an
        # impossible record cannot profit from the failure.
        b["status"] = MISSED
        b["reason"] = "delivery window closed without proof"
        self._save_bid(rfq_id, b)
        seq = int(r["award_seq"]) + 1
        nxt = ""
        while seq < len(r["order"]):
            cand = str(r["order"][seq])
            if str(self._load_bid(rfq_id, cand)["status"]) == REVEALED:
                nxt = cand
                break
            seq += 1
        bond = int(b["bond"])
        if nxt:
            r["award_seq"] = str(seq)
            r["won_by"] = nxt
            r["awarded_at"] = str(now)
            r["attempts"] = "0"
            r["passed_on"] = str(int(r["passed_on"]) + bond)
            r["reason"] = "previous winner missed the window"
            self._save(r)
            self._transfer(nxt, bond)
            return "REAWARDED"

        r["bonds_back"] = str(int(r["bonds_back"]) + bond)
        r["phase"] = REFUNDED
        r["refunded"] = str(int(r["budget"]))
        r["reason"] = "every bidder missed the window"
        self._save(r)
        self._transfer(win, bond)
        self._transfer(r["buyer"], int(r["budget"]))
        return REFUNDED

    @gl.public.write
    def cancel(self, rfq_id: str) -> str:
        """Only before reveals start, so cancelling is not a free look."""
        r = self._load(rfq_id)
        if str(r["phase"]) != BIDDING:
            raise gl.vm.UserError("RFQ is " + str(r["phase"]))
        if self._addr_hex(gl.message.sender_address).lower() != str(r["buyer"]).lower():
            raise gl.vm.UserError("Only the buyer cancels")
        if int(r["bidders"]) > 0 and self._now() >= int(r["bid_until"]):
            raise gl.vm.UserError("Bidding has closed")
        back = 0
        for addr in r["roster"]:
            back += int(self._load_bid(rfq_id, str(addr))["bond"])
        r["phase"] = CANCELLED
        r["refunded"] = str(int(r["budget"]))
        r["bonds_back"] = str(int(r["bonds_back"]) + back)
        r["reason"] = "cancelled by the buyer"
        self._save(r)
        for addr in r["roster"]:
            self._transfer(str(addr), int(self._load_bid(rfq_id, str(addr))["bond"]))
        self._transfer(r["buyer"], int(r["budget"]))

    @gl.public.view
    def get_rfq(self, rfq_id: str) -> str:
        r = self._load(rfq_id)
        out = {k: r[k] for k in ("rfq_id", "buyer", "order_ref", "marker", "sources",
                                 "bond", "budget", "bid_until", "reveal_until", "window",
                                 "phase", "bidders", "roster", "won_by", "attempts",
                                 "paid", "refunded", "forfeited", "passed_on",
                                 "bonds_back", "delivered_by", "reason", "spans")}
        return json.dumps(out)

    @gl.public.view
    def get_bid(self, rfq_id: str, bidder: str) -> str:
        b = self._load_bid(rfq_id, bidder)
        out = {k: b[k] for k in ("bidder", "commitment", "bond", "status", "reason")}
        out["price"] = b["price"] if str(b["status"]) in (REVEALED, MISSED, DELIVERED, OUTBID) else ""
        return json.dumps(out)

    @gl.public.view
    def get_order(self, rfq_id: str) -> str:
        """Ranked bidders, cheapest first, ties by address."""
        r = self._load(rfq_id)
        out = []
        for i, addr in enumerate(r["order"]):
            b = self._load_bid(rfq_id, str(addr))
            out.append({"rank": i, "bidder": addr, "price": b["price"], "status": b["status"]})
        return json.dumps({"rfq_id": str(rfq_id), "phase": r["phase"], "order": out})

    @gl.public.view
    def get_rfq_count(self) -> int:
        return int(self.rfq_count)
