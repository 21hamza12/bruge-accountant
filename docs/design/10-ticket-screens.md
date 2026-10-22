# What the ticket screenshots show (22 Oct 2026)

16 WhatsApp photos of Amadeus screens, kept in `intake/ticket-screens/` (never committed).
Passenger and client names are left out here; bookings are referred to by PNR.

## 1. They are text screens, so agents can paste instead of photographing

Every screenshot is an Amadeus **PNR display** (`--- TST RLR ---`, the booking as text) or a
ticket display, plus two photos of the **TJQ report shown on screen**. None is a graphic: it is
the terminal's text. If agents **copy and paste** that text (Selling Platform: select all, copy),
the app reads it with plain code: exact, instant, no AI, no cost. A photo then becomes only the
fallback.

## 2. What a PNR display gives

| Line | Example (shape only) | What the app takes |
|---|---|---|
| `RP/<office>/<office>  <agent>/<duty>  <date>  <PNR>` | `RP/NKC262213/NKC262213  SV/SU  21OCT26/0000Z  ZX6AGG` | office, agent initials, PNR |
| `1.NAME/FIRST  2.NAME/FIRST(CHD)` | `(CHD)`, `(INFNAME/FIRST)` | passengers and types |
| `AP NKC +222 … - BRUGE - A` | or `- ABSHER VOYAGE - A`, `- REND TOURISME ET VOYAGES - A`, `- WORLD GATE TOURISM - A` | **who booked (AG)**: BRUGE or a partner agency |
| `AP +222 …` (second AP line) | | client phone (AP) |
| `RM …` | see §3 | **Bruge price (P.B)** |
| `FA PAX 495-3940299481/ETL6/MRU5022.00/21OCT26/NKC262213/51211381/S4/P1` | `FA INF …` for infants | **ticket number, airline, cost, issue date, issuing office and IATA number, segments, passenger** |
| `FO PAX <old ticket>…` | | exchange: the ticket replaces an older one |
| `FE PAX MRU8245.00 NONREF` | | penalty / change fee |
| `FP NONREF <name>` | `NONREF CLIENT`, `NONREF ABSHER`, `NONREFWORLDGATE`, or a person's name | **who pays** (client name, partner, or "CLIENT") |
| `FA PAX 057-1949701315/DTAF/MRU12015.00/…` + `SVC … PENF / RSVT` | | **EMD** (paid extra: penalty, seat…) |

## 3. The RM line: one price **per passenger**

| PNR | RM line | Tickets | Reading |
|---|---|---|---|
| ZX6AGG | `RM 5400 MRU` | 3 adults × 5 022 | 5 400 **per passenger** (3 × 5 400 = 16 200) |
| ZOMZOM | `RM 5400 MRU ADLT 900 MRU BB` | 2 adults × 5 021, 2 infants × 497 | adult 5 400, infant ("BB") 900 |
| XEWYYC | `RM ADT = 5400 MRU , CHD = 4400 MRU` | adult 5 021, child 4 031 | price by passenger type |
| YH927B | `RM 51300 MRU` | 2 passengers × (Tunisair 21 368 + Saudia 27 878) | **layover**: 51 300 per passenger covers that passenger's 2 tickets |
| XTNFI9 | `RM 32000 MRU` | exchange of an older ticket + 3 535 collected | price of the change (to confirm) |

So the RM price is **per passenger (by type: adult / child / infant)**, and a passenger with a
layover has **two tickets for one price**. Partner bookings (AP line names a partner) usually have
**no RM line**: their price comes from the partner fee, as designed.

**Design consequence:** a "sale" (doc 05 §2) is naturally **one passenger** with all their tickets
in the PNR. A PNR with 3 passengers gives 3 sales; YH927B gives 2 sales of 2 tickets each. The
read-back can still show the whole PNR at once.

## 4. Two ticketing offices

- Bruge's own bookings: office **NKC262213**, IATA **51211381**, ticket numbers 3940299xxx. This is
  the office of the TJQ AVANT we have.
- Partner bookings (ABSHER, REND, WORLD GATE) are made in the **partners' own offices** (NKC262374,
  NKC262285, NKC262384) and ticketed by office **NKC262353**, IATA **51211845**, ticket numbers
  3940281xxx.
- **Those partner tickets will not be in NKC262213's TJQ report.** The comparison needs the TJQ of
  NKC262353 too (question 2).
- A foreign agency (office LAD282527, Angola) also books flights that Bruge tickets in NKC262213.

## 5. The TJQ on screen

Two photos show the TJQ report in the terminal (day 21 Oct, SEQ 082010–082033): same columns as the
Excel export, with the short sign (`SV`, `ER`, `AC`) instead of `0004SV`. Notable lines: a CANX and
a TKTT at 0.00 on the same PNR (cancel and reissue), a 651.00 TKTT next to two 16 853 tickets of the
same PNR (likely an added service).

## 6. Questions for the agency

1. Can agents **copy the PNR text** in their Amadeus screen (select all, copy) and paste it? If yes,
   pasting becomes the normal way and photos the fallback.
2. Partner tickets are issued in office **NKC262353**. Is there a separate TJQ for that office, and
   can it be exported daily like NKC262213's?
3. `FP NONREF <a person's name>`: is that the **client** who pays, or a reseller?
   `FP NONREF CLIENT` means a walk-in client?
4. Confirm: the RM price is **per passenger** (and per type: adult, child "CHD", infant "BB").
5. ABSHER VOYAGE, REND TOURISME ET VOYAGES, WORLD GATE TOURISM, MUNDO DE VIAGENS (Angola): all
   partners? Their current fees?
6. XTNFI9 (exchange): is `RM 32000` the price of the change, and what is the 8 245 `FE` line
   (penalty kept by the airline)?
