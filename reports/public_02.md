# Grade report — public_02_incidents

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.286 | +21.6% | 0.199 | +9.9% | 0.505 | +7.9% | 42% | 88 (348) |
| nw_kyc | 0.383 | +2.5% | 0.235 | +0.2% | 0.622 | +2.3% | 46% | 20 (29) |
| nw_renewal | 0.449 | +3.8% | 0.261 | -0.5% | 0.578 | +4.3% | 24% | 33 (54) |
| ALL | 0.361 | +9.2% | 0.227 | +3.6% | 0.557 | +4.7% | 34% | 141 (431) |

## Changes applied by the controller

18 applied — beneficial: 9, neutral: 8, harmful: 1

- ks_cancel v2 (controller_approved) dims=['prompt_version'] engageΔ=+0.031 didΔ=+0.000 → **beneficial**
- nw_renewal v2 (controller_approved) dims=['prompt_version'] engageΔ=+0.031 didΔ=+0.000 → **beneficial**
- nw_kyc v2 (controller_approved) dims=['prompt_version'] engageΔ=+0.030 didΔ=+0.000 → **beneficial**
- ks_cancel v3 (controller_approved) dims=['cohorts', 'welcome_message'] engageΔ=+0.007 didΔ=+0.000 → **neutral**
- nw_renewal v3 (controller_approved) dims=['cohorts', 'welcome_message'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v4 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v4 (controller_approved) dims=['voice'] engageΔ=+0.004 didΔ=+0.000 → **neutral**
- ks_cancel v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.181 → **beneficial**
- ks_cancel v6 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.152 → **beneficial**
- ks_cancel v7 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.070 → **beneficial**
- nw_kyc v3 (controller_approved) dims=['cohorts', 'voice'] engageΔ=+0.006 didΔ=+0.000 → **neutral**
- nw_renewal v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.027 didΔ=+0.000 → **beneficial**
- ks_cancel v9 (controller_approved) dims=['prompt_version'] engageΔ=+0.136 didΔ=+0.000 → **beneficial**
- nw_renewal v6 (controller_approved) dims=['welcome_message'] engageΔ=+0.119 didΔ=+0.000 → **beneficial**
- nw_renewal v8 (controller_approved) dims=['voice'] engageΔ=-0.004 didΔ=+0.000 → **neutral**
- ks_cancel v10 (controller_auto) dims=['welcome_message'] engageΔ=-0.007 didΔ=+0.000 → **neutral**
- ks_cancel v11 (controller_auto) dims=['voice'] engageΔ=+0.004 didΔ=+0.000 → **neutral**
- nw_renewal v9 (controller_approved) dims=['voice'] engageΔ=-0.015 didΔ=+0.000 → **harmful**

## Governance

```
{
  "approval_requests": 17,
  "decisions": {
    "approved": 15,
    "rejected": 2,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 1,
    "prompt changes need evidence.validation_run_id for this workflow and prompt": 1
  },
  "applied_via_approval": 12,
  "auto_applied": 6,
  "auto_apply_rejected": 4,
  "apply_conflicts": 3,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 2,
    "nw_kyc": 2,
    "ks_cancel": 2
  }
}
```

## Incidents

```
[
  {
    "type": "did_flagged",
    "workflow": "ks_cancel",
    "at": "D2 10:00",
    "flagged_did_share_by_day": {
      "2": 0.471,
      "3": 0.019,
      "4": 0.016,
      "5": 0.019,
      "6": 0.022,
      "7": 0.022,
      "8": 0.021,
      "9": 0.019
    }
  },
  {
    "type": "supervisor_edit",
    "workflow": "ks_cancel",
    "at": "D3 11:30",
    "mttr_minutes": 1530,
    "fixed_by": "controller_approved"
  },
  {
    "type": "supervisor_edit",
    "workflow": "nw_renewal",
    "at": "D5 15:00"
  }
]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "5": 0.846,
    "6": 0.849,
    "7": 0.842,
    "8": 0.859,
    "9": 0.839
  }
}
```

Credits used: 16 · validation runs: 5 · LLM fix calls: 2