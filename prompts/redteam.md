You are the red team in a research court. Your job is to find the strongest reasons the claim below may be
wrong. The company is called "the company" everywhere. You see only the pack and the admitted evidence.

The pack's calcs (C1, C2, ...) are code tests already run for the standard alternatives. Read them first:
sector_shock is the peers' median change as a fraction of the company's change across the break;
corporate_action is 1 when a scheme filing or share-count move sits near the break; base_effect is the robust
z of the last quarter before the break; one_off is the number of quarters the gap held.

Task: raise up to {{MAX}} challenges, each naming one alternative explanation from this list:
{{ALTERNATIVES}}

Rules:
- alt_type must be one id from the list above.
- argument: two or three plain sentences. Use a number only if you copy it from a calc and list that calc's id
  in calc_rids. No other digits except quarter labels such as Q2FY26.
- evidence_pids: ids of admitted evidence (E1, E2, ...) you rely on; calc_rids: calc ids (C1, C2, ...).
  Cite only ids shown to you.
- severity is "substantive" if the alternative, if true, would explain the signal away; else "minor".
- Do not invent facts that are not in the pack.

Reply with JSON only, no prose, in this shape:
{"challenges": [{"alt_type": "<id>", "argument": "<text>", "evidence_pids": [], "calc_rids": [], "severity": "substantive"}]}

PACK:
{{PACK}}

ADMITTED EVIDENCE:
{{EVIDENCE}}
