# Fixtures

- `1OCT.xlsx` - real Amadeus daily sales report (01OCT, generated 02-OCT-2026). Parser input.
- `1oct_hand_typed.csv` - the staff's hand-typed columns I-K from that sheet (`P.B`, `AG`, `CLT`),
  keyed by ticket number. The `AP` phone column is deliberately excluded.

The CSV is reference data for later phases (P.B is the full price when AG=BRG and only the
fee for partner rows). It is never used as input: the parser ignores those columns entirely.
Formula cells are stored as their formula text (for example `=D32`).
